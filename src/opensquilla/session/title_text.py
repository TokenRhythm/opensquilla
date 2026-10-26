"""Shared bounded transcript title text and generic chat-name recognition."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator
from typing import Any

from opensquilla.chat.flattened_tool_markers import is_flattened_tool_result_dump
from opensquilla.session.attachment_history import USER_DISPLAY_TEXT_SQL

# Title/search enrichment only needs a bounded prefix.  Keep this separate
# from the history wire preview because these helpers return plain strings and
# otherwise can pull a full legacy body across the SQLite/Python boundary.
SESSION_TITLE_PREVIEW_CHARS = 4 * 1024
SESSION_TITLE_MAX_BODY_BYTES = 64 * 1024
# 仅在 metadata 已筛出 <=64KiB 的候选后投影正文。先截 JSON 会丢掉图片
# base64 后的 display_text，必须在 SQLite 内提取可见正文再限制返回长度。
_SESSION_TITLE_CONTENT_SQL = f"""CASE WHEN json_valid(content) THEN CASE
    WHEN json_type(content, '$.display_text') = 'text'
      OR (json_type(content, '$.text') = 'text'
          AND json_type(content, '$.attachments') = 'array')
    THEN json_object('display_text',
         substr({USER_DISPLAY_TEXT_SQL}, 1, {SESSION_TITLE_PREVIEW_CHARS}))
    ELSE substr(content, 1, {SESSION_TITLE_PREVIEW_CHARS}) END
    ELSE substr(content, 1, {SESSION_TITLE_PREVIEW_CHARS}) END"""

_TIME_PREFIX_RE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+\-]\d{2}:\d{2} "
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"[A-Za-z0-9_+\-/]+\]\n"
)


def _is_regional_indicator(char: str) -> bool:
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


def _is_grapheme_extend(char: str) -> bool:
    codepoint = ord(char)
    return (
        unicodedata.category(char) in {"Mn", "Mc", "Me"}
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0xE0100 <= codepoint <= 0xE01EF
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or 0xE0020 <= codepoint <= 0xE007F
    )


def _hangul_syllable_type(char: str) -> str | None:
    """Return the UAX #29 Hangul class used by grapheme rules GB6-GB8."""

    codepoint = ord(char)
    if 0x1100 <= codepoint <= 0x115F or 0xA960 <= codepoint <= 0xA97C:
        return "L"
    if 0x1160 <= codepoint <= 0x11A7 or 0xD7B0 <= codepoint <= 0xD7C6:
        return "V"
    if 0x11A8 <= codepoint <= 0x11FF or 0xD7CB <= codepoint <= 0xD7FB:
        return "T"
    if 0xAC00 <= codepoint <= 0xD7A3:
        return "LV" if (codepoint - 0xAC00) % 28 == 0 else "LVT"
    return None


def _hangul_extends_cluster(previous: str, current: str) -> bool:
    previous_type = _hangul_syllable_type(previous)
    current_type = _hangul_syllable_type(current)
    return bool(
        (previous_type == "L" and current_type in {"L", "V", "LV", "LVT"})
        or (previous_type in {"LV", "V"} and current_type in {"V", "T"})
        or (previous_type in {"LVT", "T"} and current_type == "T")
    )


def _title_grapheme_clusters(text: str) -> Iterator[str]:
    """Yield title graphemes so truncation need not scan the remaining history."""

    cluster = ""
    join_next = False
    regional_indicators = 0
    for char in text:
        regional = _is_regional_indicator(char)
        extends_cluster = (
            bool(cluster)
            and (
                join_next
                or char == "\u200d"
                or _is_grapheme_extend(char)
                or _hangul_extends_cluster(cluster[-1], char)
                or (regional and regional_indicators == 1)
            )
        )
        if cluster and not extends_cluster:
            yield cluster
            cluster = ""
            regional_indicators = 0
        cluster += char
        regional_indicators = regional_indicators + 1 if regional else 0
        join_next = char == "\u200d" or "VIRAMA" in unicodedata.name(char, "")
    if cluster:
        yield cluster


def _truncate_title_graphemes(text: str, max_chars: int) -> str:
    suffix = "..."
    prefix_budget = max(0, max_chars - len(suffix))
    prefix: list[str] = []
    prefix_chars = 0
    for cluster in _title_grapheme_clusters(text):
        if prefix_chars + len(cluster) > prefix_budget:
            break
        prefix.append(cluster)
        prefix_chars += len(cluster)
    return "".join(prefix).rstrip() + suffix


def derive_transcript_title(content: Any, *, max_chars: int = 34) -> str:
    text = _content_text(content)
    if not text:
        return ""
    text = _TIME_PREFIX_RE.sub("", text, count=1)
    if is_flattened_tool_result_dump(text):
        return ""
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = cleaned.strip("\"'` ")
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    return _truncate_title_graphemes(cleaned, max_chars)


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        raw = content.strip()
        if not raw:
            return ""
        if raw[0] in "[{":
            try:
                return _content_text(json.loads(raw))
            except Exception:
                return raw
        return raw
    if isinstance(content, dict):
        # display_text 的空串表示用户只发了附件；不能退回 provider 补充提示，
        # 也不能递归把附件文件名/base64 当成会话标题。缺失时保留旧客户端兼容。
        display_text = content.get("display_text")
        if isinstance(display_text, str):
            return display_text.strip()
        for key in (
            "text",
            "message",
            "semantic_message",
            "prompt",
            "query",
            "content",
        ):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in content.values():
            nested = _content_text(value)
            if nested:
                return nested
        return ""
    if isinstance(content, list):
        for value in content:
            nested = _content_text(value)
            if nested:
                return nested
    return ""


def _is_generic_webchat_title(value: str, effective_agent_id: str) -> bool:
    normalized = value.strip().lower().replace("_", " ").replace("-", " ")
    generic = {
        "",
        "chat",
        "current session",
        "direct chat",
        "new chat",
        "web chat",
        "webchat",
    }
    if normalized in generic:
        return True
    return normalized == effective_agent_id.strip().lower()
