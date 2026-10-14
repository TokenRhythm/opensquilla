"""Publish a generative-frame-compatible widget directly into the chat."""

from __future__ import annotations

import json
from typing import Any

from opensquilla.artifact_session.errors import ArtifactConflictError
from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor
from opensquilla.tools.registry import tool
from opensquilla.tools.types import PlanAccess, ToolError, current_tool_context


@tool(
    name="show_widget",
    description=(
        "Show an interactive visual inline in the conversation using the "
        "generative-frame widget contract. Provide a short title "
        "and an HTML fragment or standalone SVG in widget_code. The host snapshots "
        "the widget as an internal immutable Visualize Artifact for reloads and remote clients. "
        "Showing the same title replaces that widget. "
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
            or any(len(message) > 200 for message in loading_messages if isinstance(message, str))
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
    except (ArtifactConflictError, TypeError, ValueError) as exc:
        raise ToolError(f"SHOW_WIDGET_FAILED: {exc}") from exc
    return json.dumps(result, ensure_ascii=False)


@tool(
    name="edit_widget",
    description=(
        "Change a widget already shown in the conversation using exact string "
        "replacements applied in order to its latest code. Every old_string must "
        "match exactly once; if any replacement fails, no change is applied. "
        "Use show_widget to create a missing widget."
    ),
    params={
        "title": {
            "type": "string",
            "description": (
                "The exact short title used by show_widget for this conversation widget."
            ),
        },
        "edits": {
            "type": "array",
            "description": "One or more exact replacements, applied in order.",
            "minItems": 1,
            "maxItems": 32,
            "items": {
                "type": "object",
                "properties": {
                    "old_string": {
                        "type": "string",
                        "description": "Exact text copied from the widget's latest code.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                },
                "required": ["old_string", "new_string"],
                "additionalProperties": False,
            },
        },
        "expected_version": {
            "type": "integer",
            "minimum": 1,
            "description": "Optional version fence from the latest widget receipt.",
        },
    },
    required=["title", "edits"],
    owner_only=True,
    plan_access=PlanAccess.READ_ONLY,
    cancellation_policy="must_settle",
    sandbox=SandboxToolDescriptor.artifact(kind="artifact.preview"),
)
async def edit_widget(
    title: str,
    edits: list[dict[str, str]],
    expected_version: int | None = None,
) -> str:
    """Apply exact replacements to the latest persisted widget revision."""
    context = current_tool_context.get()
    adopter = getattr(context, "generated_artifact_adopter", None) if context else None
    edit = getattr(adopter, "edit_widget", None) if adopter else None
    if context is None or not callable(edit):
        raise ToolError(
            "EDIT_WIDGET_UNAVAILABLE: This session has no authenticated Visualize Artifact "
            "publisher. Use show_widget to create a widget when available."
        )
    if not isinstance(edits, list) or not edits or len(edits) > 32:
        raise ToolError("EDIT_WIDGET_INVALID_EDITS: provide one to 32 replacements")
    if expected_version is not None and (
        type(expected_version) is not int or expected_version < 1
    ):
        raise ToolError("EDIT_WIDGET_INVALID_VERSION: expected_version must be positive")
    try:
        result: dict[str, Any] = await edit(
            context,
            title=title,
            edits=edits,
            expected_version=expected_version,
        )
    except (ArtifactConflictError, TypeError, ValueError) as exc:
        raise ToolError(f"EDIT_WIDGET_FAILED: {exc}") from exc
    return json.dumps(result, ensure_ascii=False)


__all__ = ["show_widget", "edit_widget"]
