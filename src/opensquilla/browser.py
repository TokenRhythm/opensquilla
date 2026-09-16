"""Authenticated connection to the desktop's existing browser surfaces."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

_URL_ENV = "OPENSQUILLA_DESKTOP_BROWSER_URL"
_TOKEN_ENV = "OPENSQUILLA_DESKTOP_BROWSER_TOKEN"
_MAX_RESPONSE_BYTES = 12 * 1024 * 1024
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_OPERATIONS = frozenset({"list", "open", "snapshot", "act", "screenshot", "reload"})
_NATIVE_ERROR_CODES = {
    code: f"BROWSER_{code}"
    for code in (
        "FORBIDDEN", "NOT_FOUND", "RESPONSE_TOO_LARGE", "TIMEOUT", "UNAUTHORIZED",
        "ACTION_UNAVAILABLE", "ELEMENT_NOT_FOUND", "NAVIGATION_BLOCKED", "OPEN_FAILED",
        "PAGE_CHANGED", "PAGE_NOT_READY", "SCREENSHOT_UNAVAILABLE", "SNAPSHOT_FAILED",
        "STALE_ELEMENT", "TARGET_NOT_ACTIVE", "TARGET_NOT_FOUND",
    )
} | {
    "INVALID_REQUEST": "BROWSER_REQUEST_INVALID",
    "BROWSER_UNAVAILABLE": "BROWSER_UNAVAILABLE",
}


class DesktopBrowserError(RuntimeError):
    """A bounded public error without connection credentials or raw transport data."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class DesktopBrowserClient:
    endpoint: str = field(repr=False)
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        url = urlsplit(self.endpoint)
        if (
            url.scheme != "http"
            or url.hostname not in {"127.0.0.1", "::1"}
            or not url.port
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path != "/v1/browser"
            or len(self.token) < 32
            or any(char.isspace() for char in self.token)
        ):
            raise ValueError("Desktop browser connection configuration is invalid")

    async def request(
        self,
        *,
        session_key: str,
        operation: str,
        target_ref: str | None = None,
        **arguments: Any,
    ) -> dict[str, Any]:
        if not session_key or operation not in _OPERATIONS:
            raise DesktopBrowserError("BROWSER_REQUEST_INVALID", "Invalid browser request.")
        if operation not in {"list", "open"} and not target_ref:
            raise DesktopBrowserError("BROWSER_TARGET_REQUIRED", "A browser target is required.")
        payload = {"sessionKey": session_key, "operation": operation, **arguments}
        if target_ref:
            payload["targetRef"] = target_ref
        try:
            async with httpx.AsyncClient(
                timeout=30.0,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "POST",
                    self.endpoint,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.token}"},
                ) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > _MAX_RESPONSE_BYTES:
                            raise DesktopBrowserError(
                                "BROWSER_RESPONSE_TOO_LARGE", "Browser response exceeded its limit."
                            )
                    status = response.status_code
            result = json.loads(raw)
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            raise DesktopBrowserError("BROWSER_TIMEOUT", "Browser operation timed out.") from None
        except (httpx.HTTPError, ValueError):
            raise DesktopBrowserError(
                "BROWSER_UNAVAILABLE", "The desktop browser connection is unavailable."
            ) from None
        if not isinstance(result, dict):
            raise DesktopBrowserError("BROWSER_RESPONSE_INVALID", "Invalid browser response.")
        if status != 200 or result.get("ok") is False:
            native_code = result.get("code")
            code = (
                _NATIVE_ERROR_CODES.get(native_code, "BROWSER_OPERATION_FAILED")
                if isinstance(native_code, str) else "BROWSER_OPERATION_FAILED"
            )
            raise DesktopBrowserError(code, "The browser operation could not be completed.")
        if target_ref and result.get("targetRef") != target_ref:
            raise DesktopBrowserError("BROWSER_TARGET_CHANGED", "The browser target changed.")
        if operation == "screenshot":
            validate_browser_screenshot(result)
        return result


def validate_browser_screenshot(result: dict[str, Any]) -> None:
    try:
        data = base64.b64decode(result["dataBase64"], validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise DesktopBrowserError("BROWSER_IMAGE_INVALID", "Invalid browser screenshot.") from None
    if (
        result.get("mimeType") != "image/png"
        or not data.startswith(b"\x89PNG\r\n\x1a\n")
        or len(data) > _MAX_IMAGE_BYTES
        or any(
            type(result.get(key)) is not int or not 0 < result[key] <= 16384
            for key in ("width", "height")
        )
    ):
        raise DesktopBrowserError("BROWSER_IMAGE_INVALID", "Invalid browser screenshot.")


_client_lock = threading.Lock()
_initialized = False
_client: DesktopBrowserClient | None = None


def initialize_desktop_browser() -> DesktopBrowserClient | None:
    """Consume host credentials before ordinary tool subprocesses can inherit them."""
    global _client, _initialized
    with _client_lock:
        if _initialized:
            return _client
        endpoint = os.environ.pop(_URL_ENV, None)
        token = os.environ.pop(_TOKEN_ENV, None)
        _initialized = True
        if os.environ.get("OPENSQUILLA_DESKTOP", "").lower() not in {"1", "true", "yes", "on"}:
            return None
        if endpoint is None and token is None:
            return None
        if not endpoint or not token:
            raise ValueError("Desktop browser connection configuration is incomplete")
        _client = DesktopBrowserClient(endpoint, token)
        return _client


def get_desktop_browser() -> DesktopBrowserClient | None:
    return initialize_desktop_browser()
