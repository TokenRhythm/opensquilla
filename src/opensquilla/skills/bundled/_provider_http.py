"""Security boundaries shared by bundled provider media adapters.

Authenticated provider control-plane requests and provider-returned media URLs
have different trust rules.  Control-plane requests stay on the configured
endpoint and never follow redirects.  Media downloads may use a provider CDN,
but are public-HTTPS only, DNS-pinned, revalidated on every redirect, bounded,
and receive a bearer credential only while the entire redirect chain remains
on the configured provider origin.
"""

from __future__ import annotations

import asyncio
import urllib.request
from collections.abc import Iterator
from typing import Any, NoReturn
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from opensquilla.tools.ssrf import (
    pinned_transport,
    validate_http_url_for_fetch,
)

_DEFAULT_PORTS = {"http": 80, "https": 443}
_MEDIA_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_MEDIA_REDIRECTS = 5
_MEDIA_READ_CHUNK_BYTES = 1024 * 1024
_RESPONSE_READ_CHUNK_BYTES = 64 * 1024


class ProviderHTTPError(ValueError):
    """A credential-safe provider transport or response failure."""


def _raise_without_context(error: ProviderHTTPError) -> NoReturn:
    """Raise a public transport error without retaining secret-bearing state."""

    try:
        raise error from None
    finally:
        error.__cause__ = None
        error.__context__ = None


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward an authenticated provider request through a redirect."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def open_authenticated_request(
    request: urllib.request.Request,
    *,
    timeout: float,
    proxy: str = "",
) -> Any:
    """Open exactly one authenticated request without redirect forwarding."""

    proxies = {"http": proxy, "https": proxy} if proxy else {}
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies),
        NoRedirectHandler(),
    )
    return opener.open(request, timeout=timeout)


def _http_origin(value: str) -> tuple[str, str, int] | None:
    raw = str(value or "").strip()
    if not raw or any(character.isspace() or ord(character) < 0x20 for character in raw):
        return None
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if (
        scheme not in _DEFAULT_PORTS
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or "\\" in parsed.netloc
    ):
        return None
    return scheme, host, port if port is not None else _DEFAULT_PORTS[scheme]


def same_http_origin(left: str, right: str) -> bool:
    """Return whether two strict HTTP(S) URLs identify the same origin."""

    left_origin = _http_origin(left)
    return left_origin is not None and left_origin == _http_origin(right)


def resolve_authenticated_url(value: str, *, base_url: str) -> str:
    """Resolve one provider URL and require the configured credential origin."""

    resolved = urljoin(f"{base_url.rstrip('/')}/", str(value or ""))
    if not same_http_origin(base_url, resolved):
        raise ProviderHTTPError("provider returned a cross-origin authenticated URL")
    return resolved


def iter_limited_response_chunks(
    response: object,
    *,
    max_bytes: int,
    error_message: str,
) -> Iterator[bytes]:
    """Yield bounded response chunks with declared and cumulative limits."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    headers = getattr(response, "headers", None)
    get_header = getattr(headers, "get", None)
    if callable(get_header):
        raw_length = get_header("Content-Length")
        if isinstance(raw_length, str):
            try:
                declared = int(raw_length.strip())
            except ValueError:
                declared = None
            if declared is not None and declared >= 0 and declared > max_bytes:
                raise ProviderHTTPError(error_message)

    read = getattr(response, "read", None)
    if not callable(read):
        raise ProviderHTTPError("provider returned an unreadable response")
    total = 0
    while True:
        chunk = read(min(_RESPONSE_READ_CHUNK_BYTES, max_bytes - total + 1))
        if not isinstance(chunk, bytes):
            raise ProviderHTTPError("provider returned an unreadable response")
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ProviderHTTPError(error_message)
        yield chunk


def read_limited_response(
    response: object,
    *,
    max_bytes: int,
    error_message: str,
) -> bytes:
    """Read a response with both declared and cumulative byte bounds."""

    return b"".join(
        iter_limited_response_chunks(
            response,
            max_bytes=max_bytes,
            error_message=error_message,
        )
    )


def _validated_public_https_target(url: str) -> tuple[str, list[str]]:
    """Return a fragment-free public HTTPS URL and DNS-pinned addresses."""

    raw = str(url or "").strip()
    if not raw or any(character.isspace() or ord(character) < 0x20 for character in raw):
        raise ProviderHTTPError("invalid or insecure provider media URL")
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        raise ProviderHTTPError("invalid or insecure provider media URL") from None
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or "\\" in parsed.netloc
    ):
        raise ProviderHTTPError("invalid or insecure provider media URL")

    url_host = f"[{host}]" if ":" in host else host
    netloc = f"{url_host}:{port}" if port is not None else url_host
    safe_url = urlunsplit(("https", netloc, parsed.path, parsed.query, ""))
    try:
        vetted_ips = validate_http_url_for_fetch(safe_url)
    except Exception:
        # The shared guard's detailed DNS/IP diagnostics are useful to tools,
        # but provider-returned URLs can contain signed values.  Keep this
        # adapter error deliberately generic and sever the exception chain.
        _raise_without_context(
            ProviderHTTPError("provider media URL is not public HTTPS")
        )
    if not vetted_ips:
        raise ProviderHTTPError("provider media host has no usable address")
    return safe_url, vetted_ips


def _transport_for_target(
    url: str,
    vetted_ips: list[str],
    *,
    proxy: str,
) -> httpx.AsyncBaseTransport | None:
    kwargs: dict[str, object] = {}
    if proxy:
        kwargs["proxy"] = proxy
    transport = pinned_transport(url, vetted_ips, **kwargs)
    if transport is not None:
        return transport  # type: ignore[return-value]
    if proxy:
        return httpx.AsyncHTTPTransport(proxy=proxy)
    return None


async def _download_public_https_bytes_async(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    proxy: str,
    authorization: str,
    authorization_base_url: str,
) -> bytes:
    current_url = url
    may_send_authorization = bool(
        authorization
        and authorization_base_url
        and same_http_origin(url, authorization_base_url)
    )
    for redirect_count in range(_MAX_MEDIA_REDIRECTS + 1):
        safe_url, vetted_ips = _validated_public_https_target(current_url)
        headers = {"Accept-Encoding": "identity"}
        if may_send_authorization and same_http_origin(
            safe_url,
            authorization_base_url,
        ):
            headers["Authorization"] = authorization

        transport = _transport_for_target(safe_url, vetted_ips, proxy=proxy)
        client_kwargs: dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": False,
            "trust_env": False,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("GET", safe_url, headers=headers) as response:
                if response.status_code in _MEDIA_REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise ProviderHTTPError(
                            "provider media redirect was missing a target"
                        )
                    if redirect_count >= _MAX_MEDIA_REDIRECTS:
                        raise ProviderHTTPError(
                            "provider media returned too many redirects"
                        )
                    next_url = urljoin(safe_url, location)
                    # Once a redirect leaves the credential origin, no later
                    # hop may regain the bearer credential.
                    may_send_authorization = bool(
                        may_send_authorization
                        and same_http_origin(safe_url, authorization_base_url)
                        and same_http_origin(next_url, authorization_base_url)
                    )
                    current_url = next_url
                    continue
                if response.status_code >= 400:
                    raise ProviderHTTPError(
                        f"provider media returned HTTP {response.status_code}"
                    )

                raw_length = response.headers.get("Content-Length")
                if isinstance(raw_length, str):
                    try:
                        declared = int(raw_length.strip())
                    except ValueError:
                        declared = None
                    if declared is not None and declared >= 0 and declared > max_bytes:
                        raise ProviderHTTPError(
                            "provider media exceeds download size limit"
                        )

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes(_MEDIA_READ_CHUNK_BYTES):
                    if not isinstance(chunk, bytes):
                        raise ProviderHTTPError("provider returned unreadable media")
                    total += len(chunk)
                    if total > max_bytes:
                        raise ProviderHTTPError(
                            "provider media exceeds download size limit"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
    raise ProviderHTTPError("provider media returned too many redirects")


def download_public_https_bytes(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    proxy: str = "",
    authorization: str = "",
    authorization_base_url: str = "",
) -> bytes:
    """Download bounded public HTTPS media without leaking credentials."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    try:
        return asyncio.run(
            _download_public_https_bytes_async(
                url,
                timeout=timeout,
                max_bytes=max_bytes,
                proxy=proxy,
                authorization=authorization,
                authorization_base_url=authorization_base_url,
            )
        )
    except ProviderHTTPError:
        raise
    except Exception:
        _raise_without_context(ProviderHTTPError("provider media download failed"))


__all__ = [
    "NoRedirectHandler",
    "ProviderHTTPError",
    "download_public_https_bytes",
    "iter_limited_response_chunks",
    "open_authenticated_request",
    "read_limited_response",
    "resolve_authenticated_url",
    "same_http_origin",
]
