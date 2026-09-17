"""General browser operations on session-owned desktop pages."""

from __future__ import annotations

import json
from typing import Any

from opensquilla.browser import DesktopBrowserError
from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor
from opensquilla.tools.registry import tool
from opensquilla.tools.types import PlanAccess, SafeToolError, current_tool_context


@tool(
    name="browser",
    description=(
        "Observe and interact with browser pages in this session, including the existing "
        "right-side preview. Use targetRef to identify the actual page. Snapshots provide "
        "element refs valid for that page document. Screenshots are supplied as images when "
        "the current model supports vision. Browser DOM changes do not save source files."
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
    if context.collaboration_mode == "plan" and operation in {"open", "act", "reload"}:
        raise SafeToolError("BROWSER_READ_ONLY: Planning permits browser observations only.")
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
    try:
        result = await client.request(
            session_key=context.session_key,
            operation=operation,
            target_ref=targetRef,
            **args,
        )
    except DesktopBrowserError as exc:
        raise SafeToolError(f"{exc.code}: {exc}") from None
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
