"""Provider-boundary redaction for upstream error text."""

from __future__ import annotations

from typing import Any, cast

import httpx

from opensquilla.redaction import redact_error_text

_MIN_EXACT_SECRET_LENGTH = 4


def redact_upstream_error_text(
    text: str,
    *,
    api_key: str,
    max_len: int = 200,
) -> str:
    """Bound and redact an upstream error using the exact active credential.

    Shape-only redaction cannot recognize every provider credential.  The
    adapter is the narrow boundary that still owns the concrete key, so it
    supplies that key for exact replacement before the common error policy is
    applied.
    """

    known_secrets = (api_key,) if len(api_key) >= _MIN_EXACT_SECRET_LENGTH else ()
    return redact_error_text(
        text,
        max_len=max_len,
        known_secrets=known_secrets,
    )


def redact_upstream_error_code(code: str, *, api_key: str) -> str:
    """Exact-redact a provider code without changing its classification text."""

    if len(api_key) < _MIN_EXACT_SECRET_LENGTH:
        return code
    return code.replace(api_key, "***")


def redacted_httpx_error(exc: httpx.HTTPError, *, api_key: str) -> httpx.HTTPError:
    """Clone an httpx error with redacted text while retaining its semantics."""

    message = redact_upstream_error_text(
        str(exc) or repr(exc),
        api_key=api_key,
        max_len=2000,
    )
    try:
        request = exc.request
    except RuntimeError:
        request = None
    if isinstance(exc, httpx.HTTPStatusError):
        return httpx.HTTPStatusError(
            message,
            request=exc.request,
            response=exc.response,
        )
    try:
        error_type: Any = type(exc)
        return cast("httpx.HTTPError", error_type(message, request=request))
    except TypeError:
        # Defensive fallback for a third-party httpx subclass with a custom
        # constructor.  HTTPError identity and request context are sufficient
        # for the discovery/probe classifier's transport semantics.
        return httpx.RequestError(message, request=request)
