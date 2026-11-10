"""Authenticated connection to the desktop's existing browser surfaces."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
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
        "NAVIGATION_FAILED", "NAVIGATION_CANCELLED", "RESOURCE_LIMIT", "PAGE_UNRESPONSIVE",
        "DIALOG_BLOCKED", "DIALOG_CONTROL_UNAVAILABLE", "FILE_CHOOSER_PENDING",
        "STALE_OBSERVATION", "VISUAL_TARGET_HIDDEN", "TEXT_UNAVAILABLE", "REFRESH_DEFERRED",
    )
} | {
    "INVALID_REQUEST": "BROWSER_REQUEST_INVALID",
    "BROWSER_UNAVAILABLE": "BROWSER_UNAVAILABLE",
}
_TARGET_REF = re.compile(r"page-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.ASCII)
_BEFORE_EXECUTION_CODES = frozenset({
    "BROWSER_REQUEST_INVALID", "BROWSER_FORBIDDEN", "BROWSER_UNAUTHORIZED",
    "BROWSER_NOT_FOUND", "BROWSER_NAVIGATION_BLOCKED", "BROWSER_RESOURCE_LIMIT",
})


class DesktopBrowserError(RuntimeError):
    """A bounded public error without connection credentials or raw transport data."""

    def __init__(
        self, code: str, message: str, *, target_ref: str | None = None,
        outcome: str | None = None,
    ) -> None:
        self.code = code
        self.target_ref = target_ref
        self.outcome = outcome
        super().__init__(message)

    @property
    def may_have_created_target(self) -> bool:
        return self.target_ref is not None or (
            self.outcome != "not_started" and self.code not in _BEFORE_EXECUTION_CODES
        )


def _browser_failure(
    code: str, *, operation: str, requested_target: str | None,
    details: dict[str, Any] | None = None,
) -> DesktopBrowserError:
    """Project only fixed recovery facts from the native error boundary."""
    details = details or {}
    native_target = details.get("targetRef")
    target = requested_target or native_target
    if not isinstance(target, str) or not _TARGET_REF.fullmatch(target):
        target = None
    if requested_target and native_target not in (None, requested_target):
        # A mismatched receipt cannot establish which target an operation affected.
        target = None
    outcome = details.get("outcome")
    if not isinstance(outcome, str) or outcome not in {"not_started", "unknown", "completed"}:
        outcome = "not_started" if code in _BEFORE_EXECUTION_CODES else "unknown"
    if code == "BROWSER_TARGET_NOT_FOUND":
        target = None
    message = {
        "not_started": "The browser operation did not start.",
        "unknown": "The browser operation's outcome is unknown.",
        "completed": "The browser operation finished with a failure.",
    }[outcome]
    if target:
        message += (
            f" The tab is retained: targetRef={target}."
            if native_target == target else f" Inspect targetRef={target}."
        )
    uncertain_open = operation == "open" and not requested_target and (
        target is not None or outcome != "not_started"
    )
    if uncertain_open:
        message += " A tab may already exist; do not repeat open without targetRef."
    recovery = details.get("recovery")
    if code == "BROWSER_NAVIGATION_BLOCKED":
        message += " Use an allowed URL."
    elif code == "BROWSER_REQUEST_INVALID":
        message += " Check the browser arguments; pass a plain URL, not a Markdown link."
    elif recovery == "stop":
        message += " Stop this operation."
    elif recovery in ("manual_dialog_response", "resolve_file_chooser"):
        message += " Resolve the pending browser dialog or file chooser before continuing."
    else:
        message += (
            " Run snapshot with this targetRef before further actions."
            if target else " Run list, then snapshot the existing targetRef before further actions."
        )
        if (
            recovery in ("change_url", "change_url_or_network")
            or code == "BROWSER_NAVIGATION_FAILED"
        ):
            message += " Correct the URL or network problem, then navigate that same targetRef."
    return DesktopBrowserError(code, message, target_ref=target, outcome=outcome)


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
                            raise _browser_failure(
                                "BROWSER_RESPONSE_TOO_LARGE", operation=operation,
                                requested_target=target_ref,
                            )
                    status = response.status_code
            result = json.loads(raw)
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            raise _browser_failure(
                "BROWSER_TIMEOUT", operation=operation, requested_target=target_ref,
            ) from None
        except (httpx.HTTPError, ValueError):
            raise _browser_failure(
                "BROWSER_UNAVAILABLE", operation=operation, requested_target=target_ref,
            ) from None
        if not isinstance(result, dict):
            raise _browser_failure(
                "BROWSER_RESPONSE_INVALID", operation=operation, requested_target=target_ref,
            )
        if status != 200 or result.get("ok") is False:
            native_code = result.get("code")
            code = (
                _NATIVE_ERROR_CODES.get(native_code, "BROWSER_OPERATION_FAILED")
                if isinstance(native_code, str) else "BROWSER_OPERATION_FAILED"
            )
            raise _browser_failure(
                code, operation=operation, requested_target=target_ref, details=result,
            )
        if operation == "open" and not target_ref and (
            not isinstance(result.get("targetRef"), str) or not result["targetRef"].strip()
        ):
            raise _browser_failure(
                "BROWSER_RESPONSE_INVALID", operation=operation, requested_target=None,
            )
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
