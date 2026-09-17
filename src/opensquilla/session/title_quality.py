"""Lightweight recognition of refusal text that cannot serve as an automatic title."""

from __future__ import annotations

import re

_WRAPPERS = "\"'`“”‘’「」『』《》*# \t\r\n"
_BOUNDARY_CHARS = _WRAPPERS + ".。!！?？,，;；:：、"
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})
_TITLE_PREFIX = re.compile(r"^title\s*[:：]\s*", re.IGNORECASE)
_APOLOGY = re.compile(
    r"^(?:(?:i(?:'m| am)\s+)?sorry|i apologize)"
    r"(?:\s*,?\s*but\b)?[\s.,!?:;]*"
)
_CHINESE_APOLOGY = re.compile(r"^(?:很抱歉|抱歉|对不起)[\s，。！：,!.:]*(?:但(?:是)?)?")
_REQUEST = r"(?:this|that|the|your)\s+(?:request|query|content)"
_REFUSAL = re.compile(
    r"^i(?:\s+(?:cannot|can not|can't)|(?:'m| am)\s+unable\s+to)\s+"
    r"(?:"
    r"(?:assist|help)(?:\s+you)?\s+with\s+" + _REQUEST
    + r"|provide\s+assistance\s+with\s+" + _REQUEST
    + r"|(?:generate|create|provide)\s+(?:a\s+)?"
    r"(?:(?:session|conversation)\s+)?title(?:\s+for\s+" + _REQUEST + r")?"
    r")(?:[.!?,;:].*|\s+(?:because|as)\s+.+)?$"
)
_CHINESE_REFUSAL = re.compile(
    r"^我?(?:无法|不能|不能够)(?:协助|帮助)(?:你|您)?"
    r"(?:(?:处理|完成)?(?:此|该|这个|这项|你的|您的)?(?:请求|需求|内容))?"
    r"(?:[。！？，；：.!?,;:].*)?$"
)
# One observed value persisted by the old 48-character naming limit. Do not
# extend this to arbitrary prefixes: those may be legitimate technical titles.
_LEGACY_TRUNCATED_REFUSALS = frozenset(
    {"i'm unable to provide assistance with this reque"}
)


def _normalize(text: str) -> str:
    text = " ".join(text.split())
    text = _TITLE_PREFIX.sub("", text.strip(_BOUNDARY_CHARS), count=1)
    text = text.strip(_BOUNDARY_CHARS)
    text = text.translate(_APOSTROPHES).casefold()
    return _CHINESE_APOLOGY.sub("", _APOLOGY.sub("", text, count=1), count=1)


def _matches_refusal(text: str) -> bool:
    return (
        text.rstrip(".!?。！？ ") in _LEGACY_TRUNCATED_REFUSALS
        or _REFUSAL.fullmatch(text) is not None
        or _CHINESE_REFUSAL.fullmatch(text) is not None
    )


def is_refusal_title(value: object) -> bool:
    """Recognize explicit refusal sentences, including a known persisted truncation.

    Match limited first-person refusal constructions, not individual words such
    as ``cannot`` or ``sorry``. This also accepts the full untruncated response so
    an apology on a separate line cannot conceal the refusal that follows it.
    """

    if not isinstance(value, str) or not value.strip():
        return False
    lines = [line.strip(_WRAPPERS) for line in value.splitlines()]
    if _matches_refusal(_normalize(" ".join(lines))):
        return True
    # A refusal may omit punctuation before a second line offering alternatives.
    # Only inspect the leading content, skipping standalone apology/wrapper lines;
    # a legitimate first-line topic followed by a quoted refusal stays a topic.
    for line in lines:
        text = _normalize(line)
        if text:
            return _matches_refusal(text)
    return False
