"""Profile/persona access bound to the current Agent's configured source."""

from __future__ import annotations

import json
from typing import Any

from opensquilla.agents.files import PROFILE_FILE_NAMES
from opensquilla.tools.policy_runtime import agent_file_context_allowed
from opensquilla.tools.registry import ToolRegistry, tool
from opensquilla.tools.types import ToolError, current_tool_context


def create_agent_file_tool(agent_registry: Any, *, registry: ToolRegistry | None = None) -> None:
    @tool(
        name="agent_file",
        description=(
            "Read or replace USER.md, IDENTITY.md, or SOUL.md for the current Agent. "
            "The runtime selects the source. Read first; replacement requires the returned "
            "revision. Preserve unrelated content. Existing caller authorization and tool "
            "policy apply."
        ),
        params={
            "action": {"type": "string", "enum": ["read", "replace"]},
            "name": {"type": "string", "enum": sorted(PROFILE_FILE_NAMES)},
            "content": {"type": "string", "description": "Complete replacement text."},
            "expected_revision": {
                "type": "string",
                "description": "Revision returned by read; required for replace.",
            },
        },
        required=["action", "name"],
        owner_only=True,
        default_access="deny",
        registry=registry,
    )
    async def agent_file(
        action: str, name: str, content: str | None = None, expected_revision: str | None = None,
    ) -> str:
        ctx = current_tool_context.get()
        if not agent_file_context_allowed(ctx):
            raise ToolError("Agent profile files require an authorized owner conversation.")
        assert ctx is not None
        if (
            "agent_file" in ctx.denied_tools
            or (ctx.allowed_tools is not None and "agent_file" not in ctx.allowed_tools)
            or not (
                "agent_file" in (ctx.surfaced_tools or ())
                or "agent_file" in (ctx.allowed_tools or ())
                or "agent_file" in ctx.explicitly_allowed_tools
            )
        ):
            raise ToolError("agent_file is unavailable under the current tool permissions.")
        if name not in PROFILE_FILE_NAMES:
            raise ToolError("Only USER.md, IDENTITY.md, and SOUL.md are supported.")
        if action == "read":
            result = await agent_registry.get_agent_file(ctx.agent_id, name, missing_ok=True)
        elif action == "replace":
            if not isinstance(content, str):
                raise ToolError("replace requires content")
            if not isinstance(expected_revision, str) or not expected_revision:
                raise ToolError("Read the file first and supply expected_revision for replace.")

            def invalidate() -> None:
                if ctx.on_bootstrap_source_write is not None:
                    ctx.on_bootstrap_source_write(ctx.agent_id, name)

            result = await agent_registry.set_agent_file(
                ctx.agent_id, name, content, expected_revision=expected_revision,
                on_write=invalidate,
            )
        else:
            raise ToolError("action must be read or replace")
        return json.dumps(result, ensure_ascii=False)
