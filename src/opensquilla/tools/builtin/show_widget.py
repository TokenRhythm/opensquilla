"""Publish a generative-frame-compatible widget directly into the chat."""

from __future__ import annotations

import json
from typing import Any

from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor
from opensquilla.tools.registry import tool
from opensquilla.tools.types import PlanAccess, ToolError, current_tool_context


@tool(
    name="show_widget",
    description=(
        "Show an interactive visual inline in the conversation using the "
        "generative-frame widget contract. Provide a short title "
        "and an HTML fragment or standalone SVG in widget_code. The host snapshots "
        "the widget as an immutable Visualize Artifact for reloads and remote clients. "
        "This tool does not write a workspace HTML file. Use open_workspace_preview "
        "when the user asks for an editable or multi-file webpage."
    ),
    params={
        "title": {
            "type": "string",
            "description": (
                "Short human-readable title for the widget, unique in this conversation."
            ),
        },
        "loading_messages": {
            "type": "array",
            "description": "One to four short status lines shown while the widget is prepared.",
            "items": {"type": "string"},
            "maxItems": 4,
        },
        "widget_code": {
            "type": "string",
            "description": (
                "HTML fragment (without doctype/html/head/body) or code starting with "
                "<svg for a standalone SVG. Maximum 1 MiB UTF-8."
            ),
        },
    },
    required=["title", "widget_code"],
    owner_only=True,
    plan_access=PlanAccess.READ_ONLY,
    cancellation_policy="must_settle",
    sandbox=SandboxToolDescriptor.artifact(kind="artifact.preview"),
)
async def show_widget(
    title: str,
    widget_code: str,
    loading_messages: list[str] | None = None,
) -> str:
    """Validate and publish a widget through the admitted Artifact adopter."""
    context = current_tool_context.get()
    adopter = getattr(context, "generated_artifact_adopter", None) if context else None
    publish = getattr(adopter, "publish_widget", None)
    if context is None or not callable(publish):
        raise ToolError(
            "SHOW_WIDGET_UNAVAILABLE: This session has no authenticated Visualize Artifact "
            "publisher. Use open_workspace_preview when a workspace is available."
        )
    if loading_messages is not None:
        if (
            not isinstance(loading_messages, list)
            or len(loading_messages) > 4
            or any(
                not isinstance(message, str) or not message.strip()
                for message in loading_messages
            )
        ):
            raise ToolError(
                "SHOW_WIDGET_INVALID_LOADING_MESSAGES: provide one to four nonempty strings"
            )
    try:
        result: dict[str, Any] = await publish(
            context,
            title=title,
            widget_code=widget_code,
            loading_messages=loading_messages,
        )
    except (TypeError, ValueError) as exc:
        raise ToolError(f"SHOW_WIDGET_FAILED: {exc}") from exc
    return json.dumps(result, ensure_ascii=False)


__all__ = ["show_widget"]
