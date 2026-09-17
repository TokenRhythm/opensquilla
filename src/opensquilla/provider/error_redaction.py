"""Provider-boundary redaction for upstream error text."""

from __future__ import annotations

from typing import Any, cast

import httpx

from opensquilla.redaction import redact_error_text

from .tokenrhythm_correlation import (
    TOKENRHYTHM_INSTALL_ID_HEADER,
    redact_tokenrhythm_install_ids,
)

_MIN_EXACT_SECRET_LENGTH = 4
_PRESENT_HEADER_VALUE = "[PRESENT]"


def _redact_exact_secrets(text: str, *, api_key: str) -> str:
    """Redact unbounded text held by an exception object."""

    text = redact_tokenrhythm_install_ids(text)
    if len(api_key) >= _MIN_EXACT_SECRET_LENGTH:
        text = text.replace(api_key, "***")
    return text


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

    text = redact_tokenrhythm_install_ids(text)
    known_secrets = (api_key,) if len(api_key) >= _MIN_EXACT_SECRET_LENGTH else ()
    return redact_error_text(
        text,
        max_len=max_len,
        known_secrets=known_secrets,
    )


def redact_upstream_error_code(code: str, *, api_key: str) -> str:
    """Exact-redact a provider code without changing its classification text."""

    return _redact_exact_secrets(code, api_key=api_key)


def _redacted_httpx_headers(
    headers: httpx.Headers,
    *,
    api_key: str,
) -> list[tuple[str, str]]:
    """Clone HTTP headers without retaining exact provider-bound secrets."""

    safe_headers: list[tuple[str, str]] = []
    install_id_header = TOKENRHYTHM_INSTALL_ID_HEADER.lower()
    for name, value in headers.multi_items():
        safe_name = _redact_exact_secrets(name, api_key=api_key)
        safe_value = (
            _PRESENT_HEADER_VALUE
            if name.lower() == install_id_header
            else _redact_exact_secrets(value, api_key=api_key)
        )
        safe_headers.append((safe_name, safe_value))
    return safe_headers


def _redacted_httpx_content(content: bytes, *, api_key: str) -> bytes:
    """Exact-redact arbitrary HTTP bytes while preserving non-secret bytes."""

    # Latin-1 is a lossless byte-to-text mapping. Install ids and header
    # credentials are constrained to header-safe characters, so replacement
    # remains reliable even when the surrounding payload is not UTF-8.
    text = content.decode("latin-1")
    return _redact_exact_secrets(text, api_key=api_key).encode("latin-1")


def _request_content(request: httpx.Request) -> bytes:
    try:
        return request.content
    except (httpx.RequestNotRead, httpx.StreamConsumed):
        return b""


def _response_content(response: httpx.Response) -> bytes:
    try:
        return response.content
    except (httpx.ResponseNotRead, httpx.StreamConsumed):
        return b""


def _redacted_httpx_request(
    request: httpx.Request | None,
    *,
    api_key: str,
) -> httpx.Request | None:
    if request is None:
        return None
    url = _redact_exact_secrets(str(request.url), api_key=api_key)
    return httpx.Request(
        request.method,
        url,
        headers=_redacted_httpx_headers(request.headers, api_key=api_key),
        content=_redacted_httpx_content(_request_content(request), api_key=api_key),
    )


def _redacted_httpx_response(
    response: httpx.Response,
    *,
    request: httpx.Request,
    api_key: str,
) -> httpx.Response:
    return httpx.Response(
        response.status_code,
        headers=_redacted_httpx_headers(response.headers, api_key=api_key),
        content=_redacted_httpx_content(_response_content(response), api_key=api_key),
        request=request,
    )


def _scrub_original_httpx_error(
    exc: httpx.HTTPError,
    *,
    message: str,
    request: httpx.Request | None,
    response: httpx.Response | None = None,
) -> None:
    """Remove secrets from an exception retained as ``__context__``."""

    exc.args = (message,)
    if hasattr(exc, "_request"):
        exc._request = request  # type: ignore[attr-defined]  # httpx stores it here
    if isinstance(exc, httpx.HTTPStatusError) and response is not None:
        exc.response = response
    exc.__cause__ = None
    exc.__context__ = None
    exc.__traceback__ = None


def redacted_httpx_error(exc: httpx.HTTPError, *, api_key: str) -> httpx.HTTPError:
    """Clone an httpx error without retaining secret request/response state."""

    message = redact_upstream_error_text(
        str(exc) or repr(exc),
        api_key=api_key,
        max_len=2000,
    )
    try:
        original_request = exc.request
    except RuntimeError:
        original_request = None
    request = _redacted_httpx_request(original_request, api_key=api_key)
    if isinstance(exc, httpx.HTTPStatusError):
        if request is None:
            request = httpx.Request("GET", "https://redacted.invalid/")
        response = _redacted_httpx_response(
            exc.response,
            request=request,
            api_key=api_key,
        )
        _scrub_original_httpx_error(
            exc,
            message=message,
            request=request,
            response=response,
        )
        return httpx.HTTPStatusError(
            message,
            request=request,
            response=response,
        )
    _scrub_original_httpx_error(exc, message=message, request=request)
    try:
        error_type: Any = type(exc)
        return cast("httpx.HTTPError", error_type(message, request=request))
    except TypeError:
        # Defensive fallback for a third-party httpx subclass with a custom
        # constructor.  HTTPError identity and request context are sufficient
        # for the discovery/probe classifier's transport semantics.
        return httpx.RequestError(message, request=request)
