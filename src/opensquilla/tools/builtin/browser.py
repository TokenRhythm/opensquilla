"""General browser operations on session-owned desktop pages."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from opensquilla.browser import DesktopBrowserError
from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor
from opensquilla.tools.registry import tool
from opensquilla.tools.types import PlanAccess, SafeToolError, current_tool_context


def _open_url_key(url: str | None) -> str:
    value = url or "about:blank"
    try:
        # Native HTTP URL parsing trims ASCII spaces. Other surrounding
        # whitespace must remain distinct; native validation rejects controls.
        parsed = urlsplit(value.strip(" "))
        if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
            host = parsed.hostname.lower()
            if ":" in host:
                host = f"[{host}]"
            port = parsed.port
            if port is not None and port != {"http": 80, "https": 443}[parsed.scheme.lower()]:
                host += f":{port}"
            # Keep credentials distinct even though navigation rejects them.
            if "@" in parsed.netloc:
                host = parsed.netloc.rsplit("@", 1)[0] + "@" + host
            value = urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, ""))
    except ValueError:
        pass  # Native argument validation remains authoritative.
    return hashlib.sha256(value.encode()).hexdigest()


@tool(
    name="browser",
    description=(
        "Observe and interact with browser pages in this session, including the existing "
        "right-side preview. Use targetRef to identify the actual page. "
        "Prefer the desktop-browser MCP tools for URL pages when available; use this tool "
        "for workspace artifact previews. "
        "Opening a new page without a URL creates a blank page; navigating an existing "
        "targetRef requires a URL. "
        "If open has an unknown outcome or retains a tab, list pages, then snapshot "
        "the retained targetRef and navigate that target if needed. Do not repeat an "
        "unresolved open without targetRef; new-tab retries are blocked for this turn. "
        "Correct argument errors before retrying operations that did not start. "
        "Snapshots provide element refs valid for that page document. Screenshots are supplied "
        "as images when the current model supports vision. "
        "Browser DOM changes do not save source files."
    ),
    params={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "list",
                    "open",
                    "snapshot",
                    "act",
                    "screenshot",
                    "reload",
                ],
            },
            "targetRef": {"type": "string"},
            "url": {"type": "string"},
            "action": {
                "type": "string",
                "enum": [
                    "click",
                    "fill",
                    "press",
                    "scroll",
                    "hover",
                    "select",
                ],
            },
            "ref": {"type": "string"},
            "text": {"type": "string"},
            "key": {"type": "string"},
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            "amount": {"type": "integer", "minimum": 0, "maximum": 10000},
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
    owner_only=True,
    plan_access=PlanAccess.READ_ONLY,
    runtime_only_arguments={"_tool_use_id"},
    sandbox=SandboxToolDescriptor.custom(kind="browser"),
)
async def browser(
    operation: str,
    targetRef: str | None = None,  # noqa: N803 - public wire field
    url: str | None = None,
    action: str | None = None,
    ref: str | None = None,
    text: str | None = None,
    key: str | None = None,
    direction: str | None = None,
    amount: int | None = None,
    _tool_use_id: str = "",
) -> str:
    context = current_tool_context.get()
    client = getattr(context, "desktop_browser", None)
    if context is None or client is None or not context.session_key or not context.is_owner:
        raise SafeToolError(
            "BROWSER_UNAVAILABLE: No browser connection is available for this session."
        )
    args: dict[str, Any] = {
        k: v
        for k, v in {
            "url": url,
            "action": action,
            "ref": ref,
            "text": text,
            "key": key,
            "direction": direction,
            "amount": amount,
        }.items()
        if v is not None
    }
    attempt_key = None
    if operation == "open" and not targetRef:
        attempt_key = (context.execution_id, context.session_key, _open_url_key(url))
        prior = context.browser_open_attempts.get(attempt_key)
        if prior:
            raise SafeToolError(f"BROWSER_OPEN_UNRESOLVED: {prior}")
        # Admission precedes the first await so concurrent opens cannot race.
        pending_message = (
            "An earlier open may already have created a tab. Do not repeat open without "
            "targetRef. Run list, then snapshot the existing targetRef and navigate that "
            "same target if needed."
        )
        context.browser_open_attempts[attempt_key] = pending_message
        context.turn_cleanup_callbacks.append(
            lambda: context.browser_open_attempts.pop(attempt_key, None)
        )
    try:
        result = await client.request(
            session_key=context.session_key,
            operation=operation,
            target_ref=targetRef,
            **args,
        )
    except DesktopBrowserError as exc:
        if attempt_key:
            if exc.may_have_created_target:
                context.browser_open_attempts[attempt_key] = str(exc)
            else:
                context.browser_open_attempts.pop(attempt_key, None)
        raise SafeToolError(f"{exc.code}: {exc}") from None
    except asyncio.CancelledError:
        # Cancellation does not prove that the native side stopped before creation.
        raise
    else:
        if attempt_key:
            context.browser_open_attempts.pop(attempt_key, None)
    if operation == "screenshot":
        encoded = result.pop("dataBase64")
        if _tool_use_id:
            context.tool_result_media[_tool_use_id] = [
                {
                    "mime": "image/png",
                    "data": encoded,
                    "width": result["width"],
                    "height": result["height"],
                }
            ]
        result["imageAvailable"] = bool(_tool_use_id)
        result["imageNote"] = "Image delivery depends on the current model's vision capability."
    return json.dumps(result, ensure_ascii=False)
