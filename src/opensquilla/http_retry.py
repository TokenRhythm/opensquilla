"""Shared parsing for HTTP retry-pacing headers.

Neutral ground: both the LLM provider layer and the chat-channel layer talk
HTTP to third parties that pace them with ``Retry-After``, and neither should
depend on the other to read a header. The parsing rules are RFC 9110's, not
any one provider's.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

__all__ = ["parse_retry_after"]


def parse_retry_after(
    value: str | None,
    *,
    now_utc: datetime | None = None,
) -> float | None:
    """Parse a ``Retry-After`` header value into non-negative seconds.

    Accepts both RFC 9110 forms: delta-seconds (``"120"``; fractional values
    are tolerated) and HTTP-date (``"Wed, 21 Oct 2026 07:28:00 GMT"``, resolved
    against ``now_utc`` — wall clock — at parse time so the caller can keep
    working in relative/monotonic seconds afterwards). Returns ``None`` for a
    missing, empty, negative, non-finite, or unparseable value; a past
    HTTP-date parses to ``0.0``.

    Callers are expected to cap the result: a hostile or broken header must
    not park a deployment for hours.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        seconds = None
    if seconds is not None:
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return seconds
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    reference = now_utc if now_utc is not None else datetime.now(UTC)
    return max(0.0, (when - reference).total_seconds())
