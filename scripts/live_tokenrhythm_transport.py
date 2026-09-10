"""Explicit test-process HTTPX mounts for the real-provider budget relay.

The product keeps its official provider URL, routing, request body and timeouts.
Only the network transport changes. This module is never imported by product
code; an isolated acceptance process opts in through temporary sitecustomize.
"""

from __future__ import annotations

import functools
import ipaddress
import os
from collections.abc import Callable, Mapping
from typing import Any

import httpx

_OFFICIAL_HOST = "tokenrhythm.studio"
_PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
_AUTH_HEADERS = {"authorization", "x-api-key", "api-key", "proxy-authorization", "cookie"}


class TransportRejectedError(httpx.TransportError):
    """A test transport request cannot safely reach its requested destination."""


class RelayTarget:
    def __init__(self, base_url: str, client_key: str) -> None:
        url = httpx.URL(base_url)
        if (
            url.scheme != "http"
            or url.host != "127.0.0.1"
            or url.port is None
            or url.path.rstrip("/") != "/v1"
            or url.query
            or url.fragment
            or url.userinfo
        ):
            raise TransportRejectedError("invalid_loopback_relay")
        if not client_key.startswith("live-budget-placeholder-") or len(client_key) < 40:
            raise TransportRejectedError("invalid_placeholder_credential")
        self.url = url
        self.client_key = client_key

    def project(self, request: httpx.Request) -> httpx.Request:
        url = request.url
        if url.scheme not in {"http", "https"}:
            raise TransportRejectedError("unsupported_network_scheme")
        if url.host == _OFFICIAL_HOST:
            if url.scheme != "https" or url.port not in {None, 443} or url.query:
                raise TransportRejectedError("unsupported_provider_endpoint")
            permitted = (request.method, url.path) in {
                ("POST", "/v1/chat/completions"),
                ("GET", "/v1/models"),
            }
            if permitted:
                if request.headers.get("Authorization") != "Bearer " + self.client_key:
                    raise TransportRejectedError("unexpected_provider_credential")
                headers = request.headers.copy()
                headers["Host"] = self.url.netloc.decode("ascii")
                return httpx.Request(
                    request.method,
                    self.url.copy_with(path=url.path),
                    headers=headers,
                    stream=request.stream,
                    extensions=dict(request.extensions),
                )
            if request.method != "GET" or url.path != "/api/models":
                raise TransportRejectedError("unsupported_provider_endpoint")
        # A placeholder can never leave the test relay, including via redirects.
        if self.client_key in str(url) or any(
            self.client_key in value for value in request.headers.values()
        ):
            raise TransportRejectedError("placeholder_outside_relay")
        try:
            local = ipaddress.ip_address(url.host).is_loopback
        except ValueError:
            local = url.host == "localhost"
        if local:
            return request
        # Public page/catalog reads remain available to ordinary tools. Other
        # provider hosts, hosted tools and authenticated fallbacks fail closed.
        if request.method not in {"GET", "HEAD"} or any(
            name.lower() in _AUTH_HEADERS for name in request.headers
        ):
            raise TransportRejectedError("unbudgeted_external_request")
        if url.userinfo:
            raise TransportRejectedError("unbudgeted_external_credential")
        return request


class BudgetedAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(
        self, target: RelayTarget, downstream: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.target = target
        self.downstream = downstream or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.downstream.handle_async_request(self.target.project(request))

    async def aclose(self) -> None:
        await self.downstream.aclose()


class BudgetedTransport(httpx.BaseTransport):
    def __init__(self, target: RelayTarget, downstream: httpx.BaseTransport | None = None) -> None:
        self.target = target
        self.downstream = downstream or httpx.HTTPTransport(retries=0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self.downstream.handle_request(self.target.project(request))

    def close(self) -> None:
        self.downstream.close()


def install_from_env(environ: Mapping[str, str] | None = None) -> Callable[[], None]:
    """Install mounts only after explicit opt-in; return a test cleanup hook.

    Startup must stop on rejection. A temporary sitecustomize caller should
    convert installation errors to SystemExit, since Python ignores ordinary
    sitecustomize exceptions. Existing proxy/custom-transport configurations
    require a separate reviewed setup instead of silently bypassing this gate.
    """
    env = os.environ if environ is None else environ
    if env.get("OPENSQUILLA_LIVE_TRANSPORT") != "1":
        raise TransportRejectedError("live_transport_disabled")
    if any(env.get(name) or env.get(name.lower()) for name in _PROXY_NAMES):
        raise TransportRejectedError("unsupported_proxy_environment")
    placeholder = env.get("OPENSQUILLA_LIVE_RELAY_CLIENT_KEY")
    if env.get("TOKENRHYTHM_API_KEY") not in {None, "", placeholder}:
        raise TransportRejectedError("real_credential_must_stay_in_relay")
    target = RelayTarget(
        env.get("OPENSQUILLA_LIVE_RELAY_URL", ""),
        env.get("OPENSQUILLA_LIVE_RELAY_CLIENT_KEY", ""),
    )
    original_async = httpx.AsyncClient.__init__
    original_sync = httpx.Client.__init__

    def check_options(kwargs: dict[str, Any]) -> None:
        if kwargs.get("proxy") or kwargs.get("mounts") or kwargs.get("transport"):
            raise TransportRejectedError("unsupported_custom_http_transport")
        # Also check ambient env on each client creation: later mutation must
        # not install a more-specific HTTPX proxy/no-proxy mount around the gate.
        if any(os.environ.get(name) or os.environ.get(name.lower()) for name in _PROXY_NAMES):
            raise TransportRejectedError("unsupported_proxy_environment")

    @functools.wraps(original_async)
    def async_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        check_options(kwargs)
        kwargs["mounts"] = {"all://": BudgetedAsyncTransport(target)}
        original_async(self, *args, **kwargs)

    @functools.wraps(original_sync)
    def sync_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        check_options(kwargs)
        kwargs["mounts"] = {"all://": BudgetedTransport(target)}
        original_sync(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = async_init
    httpx.Client.__init__ = sync_init

    def restore() -> None:
        httpx.AsyncClient.__init__ = original_async
        httpx.Client.__init__ = original_sync

    return restore
