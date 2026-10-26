"""Private desktop account bridge. Long-lived account/model keys never enter Python.

The model adapter keeps the official service URL as its authority. Only the
HTTP transport substitutes the authenticated, fixed-upstream loopback proxy.
"""

from __future__ import annotations

import contextvars
import http.client
import json
import os
import re
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import Any
from urllib.parse import urlsplit

import httpx

_URL_ENV = "OPENSQUILLA_DESKTOP_ACCOUNT_URL"
_TOKEN_ENV = "OPENSQUILLA_DESKTOP_ACCOUNT_TOKEN"
_lock = threading.RLock()
_initialized = False
_endpoint = ""
_token = ""
_cached: dict[str, Any] | None = None
_cached_at = 0.0
_leases: dict[str, str] = {}
_turn_state: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "desktop_account_turn", default=None
)
OFFICIAL_BASE = "https://tokenrhythm.studio/v1"


@dataclass(frozen=True)
class AccountCredential:
    ready: bool
    secret: str = field(default="", repr=False)
    account_id: str = ""
    reason: str = "account_login_required"
    origin: str = "https://tokenrhythm.studio"


def initialize_desktop_account() -> None:
    global _initialized, _endpoint, _token
    with _lock:
        if _initialized:
            return
        _initialized = True
        endpoint = os.environ.pop(_URL_ENV, "")
        token = os.environ.pop(_TOKEN_ENV, "")
        if not endpoint and not token:
            return
        parsed = urlsplit(endpoint)
        if (
            os.environ.get("OPENSQUILLA_DESKTOP", "").lower() not in {"1", "true", "yes", "on"}
            or parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or not parsed.port
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
        ):
            raise ValueError("Invalid desktop account bridge")
        _endpoint, _token = endpoint, token


def desktop_account_available() -> bool:
    return bool(_endpoint and _token)


def account_state(*, force: bool = False) -> dict[str, Any] | None:
    global _cached, _cached_at
    pinned = _turn_state.get()
    if pinned is not None and not force:
        return pinned
    if not desktop_account_available():
        return None
    with _lock:
        if not force and _cached is not None and time.monotonic() - _cached_at < 0.5:
            return dict(_cached)
        parsed = urlsplit(_endpoint)
        connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=3)
        try:
            connection.request("GET", "/state", headers={"Authorization": f"Bearer {_token}"})
            response = connection.getresponse()
            payload = response.read(64 * 1024 + 1)
            if response.status != 200 or len(payload) > 64 * 1024:
                raise ValueError("Account bridge unavailable")
            state = json.loads(payload)
            if not isinstance(state, dict) or state.get("source") not in {"manual", "account"}:
                raise ValueError("Invalid account bridge state")
            lease = state.get("credential", "")
            if lease:
                if not isinstance(lease, str) or not re.fullmatch(
                    r"os_local_[A-Za-z0-9_-]{43}", lease
                ):
                    raise ValueError("Invalid account bridge credential")
                _leases[lease] = _endpoint
            _cached = state
        except (OSError, ValueError, http.client.HTTPException):
            # Preserve the selected source on bridge loss. Never silently
            # charge the manual key when an account selection cannot be read.
            _cached = {**(_cached or {"source": "account"}), "ready": False, "credential": ""}
        finally:
            connection.close()
        _cached_at = time.monotonic()
        return dict(_cached)


def pin_account_state(state: dict[str, Any] | None) -> contextvars.Token:
    return _turn_state.set(state)


def account_state_for_turn() -> dict[str, Any] | None:
    # Nested agent turns belong to the parent's in-flight operation. A new
    # top-level turn refreshes the bridge; child turns retain its source.
    return account_state(force=_turn_state.get() is None)


@contextmanager
def manual_credential_scope() -> Iterator[None]:
    """Inspect a saved manual connection without activating it."""
    token = _turn_state.set(
        {"source": "manual", "mediaSources": {"image": "manual", "video": "manual"}}
    )
    try:
        yield
    finally:
        _turn_state.reset(token)


def media_connection_operation[**P, R](
    kind: str,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(operation: Callable[P, R]) -> Callable[P, R]:
        @wraps(operation)
        def run(*args: P.args, **kwargs: P.kwargs) -> R:
            state = account_state()
            if state and state.get("mediaSources", {}).get(kind) == "manual":
                with manual_credential_scope():
                    return operation(*args, **kwargs)
            return operation(*args, **kwargs)

        return run

    return decorate


def manual_connection_operation[**P, R](
    operation: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    @wraps(operation)
    async def run(*args: P.args, **kwargs: P.kwargs) -> R:
        with manual_credential_scope():
            return await operation(*args, **kwargs)

    return run


def reset_account_state(token: contextvars.Token) -> None:
    _turn_state.reset(token)


def account_credential(
    provider: str, *, kind: str = "text", explicit: bool = False
) -> AccountCredential | None:
    if provider.strip().lower() != "tokenrhythm":
        return None
    state = account_state()
    if state is None:
        return None
    source = state.get("source", "manual")
    if kind in {"image", "video"}:
        media = state.get("mediaSources", {}).get(kind, "inherit")
        if media == "manual" or media == "inherit" and explicit:
            return None
        if media != "inherit":
            source = media
    if source != "account":
        return None
    binding = (
        state.get("mediaAccountIds", {}).get(kind)
        if kind in {"image", "video"} and state.get("mediaSources", {}).get(kind) == "account"
        else state.get("selectedAccountId")
    )
    account_id = str(binding or "")
    ready = bool(
        state.get("ready")
        and account_id
        and account_id == state.get("accountId")
        and state.get("credential")
    )
    return AccountCredential(
        ready=ready,
        secret=str(state["credential"]) if ready else "",
        account_id=account_id,
        reason="" if ready else "account_model_unavailable",
        origin=str(state.get("origin") or "https://tokenrhythm.studio").rstrip("/"),
    )


def is_account_credential(value: str) -> bool:
    return str(value).startswith("os_local_")


class AccountProxyTransport(httpx.AsyncBaseTransport):
    def __init__(self, credential: str) -> None:
        self._credential = credential
        self._endpoint = _leases.get(credential, "")
        self._transport = httpx.AsyncHTTPTransport(trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if (
            not self._endpoint
            or url.scheme != "https"
            or url.host != "tokenrhythm.studio"
            or url.port not in {None, 443}
            or url.username
            or url.password
            or url.query
        ):
            raise httpx.TransportError("Account model transport is unavailable")
        path = url.path
        allowed = (
            request.method == "GET"
            and (
                path in {"/v1/models", "/api/models"}
                or re.fullmatch(r"/v1/videos/[A-Za-z0-9][A-Za-z0-9._-]{0,127}", path)
            )
            or request.method == "POST"
            and path in {"/v1/chat/completions", "/v1/images/generations", "/v1/videos/generations"}
        )
        if not allowed:
            raise httpx.TransportError("Account model route is not supported")
        headers = httpx.Headers(request.headers)
        forwarded_url = httpx.URL(self._endpoint + path)
        # With an explicit stream HTTPX does not generate a Host header.
        # Replace the upstream authority with the loopback proxy's authority.
        headers["host"] = forwarded_url.netloc.decode("ascii")
        headers["authorization"] = f"Bearer {self._credential}"
        forwarded = httpx.Request(
            request.method,
            forwarded_url,
            headers=headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        try:
            return await self._transport.handle_async_request(forwarded)
        except httpx.HTTPError:
            raise httpx.TransportError("Account model proxy request failed") from None

    async def aclose(self) -> None:
        await self._transport.aclose()


def account_http_client(api_key: str, **kwargs: Any) -> httpx.AsyncClient:
    if is_account_credential(api_key):
        kwargs.pop("proxy", None)
        kwargs["trust_env"] = False
        kwargs["follow_redirects"] = False
        kwargs["transport"] = AccountProxyTransport(api_key)
    return httpx.AsyncClient(**kwargs)
