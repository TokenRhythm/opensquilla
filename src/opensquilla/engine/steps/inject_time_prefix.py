"""Per-turn time-prefix stamping for user messages.

Format: ``[YYYY-MM-DDTHH:MM±HH:MM Day TZ_NAME]\\n{user text}``
"""

from __future__ import annotations

import re
from datetime import datetime

TIME_PREFIX_RE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+\-]\d{2}:\d{2} "
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"[A-Za-z0-9_+\-/]+\]\n"
)


def format_time_prefix(now: datetime, tz_name: str) -> str:
    """Render the stamp line (no trailing newline)."""
    offset = now.strftime("%z")
    offset_formatted = f"{offset[:3]}:{offset[3:]}" if offset else "+00:00"
    return f"[{now.strftime('%Y-%m-%dT%H:%M')}{offset_formatted} {now.strftime('%a')} {tz_name}]"


def stamp(content: object, now: datetime, tz_name: str) -> object:
    """Prepend a time-prefix line; idempotent, skips non-strings and empty text."""
    if not isinstance(content, str) or not content.strip():
        return content
    if TIME_PREFIX_RE.match(content):
        return content
    return f"{format_time_prefix(now, tz_name)}\n{content}"
