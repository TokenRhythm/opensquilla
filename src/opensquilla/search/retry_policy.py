"""Shared retry classification for search-provider HTTP responses."""

from __future__ import annotations


def is_retryable_http_status(status_code: int) -> bool:
    """Return whether a failed HTTP response is transient enough for fallback.

    Providers perform one network request per search. This flag is consumed by
    the search orchestrator when deciding whether another provider may be tried;
    it does not trigger an in-provider retry.
    """

    return status_code in {408, 429} or 500 <= status_code < 600
