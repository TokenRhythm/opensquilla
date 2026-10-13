"""Visibility and execution policy for managed Desktop browser tools."""

from __future__ import annotations

from dataclasses import replace

from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor
from opensquilla.tools.types import CallerKind, InteractionMode, PlanAccess, ToolContext, ToolSpec

BROWSER_MCP_REQUIRED_TOOLS = frozenset(
    {
        "browser_tabs",
        "browser_open",
        "browser_navigate",
        "browser_reload",
        "browser_inspect",
        "browser_act",
        "browser_screenshot",
    }
)
BROWSER_MCP_OPTIONAL_TOOLS = frozenset(
    {
        "browser_observe",
        "browser_batch",
        "browser_handle_dialog",
        "browser_tab",
    }
)
# Existing Desktop installations advertise only the required tools. New tools
# are admitted individually, without granting an unknown server tool authority.
BROWSER_MCP_TOOLS = BROWSER_MCP_REQUIRED_TOOLS | BROWSER_MCP_OPTIONAL_TOOLS
BROWSER_MCP_TOOL_NAMES = frozenset(f"mcp__desktop-browser__{name}" for name in BROWSER_MCP_TOOLS)


def browser_context_available(context: ToolContext | None) -> bool:
    return bool(
        context is not None
        and context.desktop_browser is not None
        and context.session_key
        and context.is_owner
        and not context.guest_safe
        and context.caller_kind is CallerKind.WEB
        and context.interaction_mode is InteractionMode.INTERACTIVE
        and context.subagent_depth == 0
    )


def browser_network_policy_supported(context: ToolContext) -> bool:
    from opensquilla.sandbox.integration import active_sandbox_policy
    from opensquilla.tools.run_mode import full_host_access_for_context

    if full_host_access_for_context(context):
        return True
    network = active_sandbox_policy().network
    # The attached renderer does not use the Gateway's managed network proxy.
    # Reject explicit restrictions until they can cover redirects/subresources,
    # rather than checking only the initial navigation URL.
    return not (network.block_all_network or network.deny_domains)


def browser_tool_spec(tool_name: str, spec: ToolSpec) -> ToolSpec:
    if tool_name not in BROWSER_MCP_TOOLS:
        raise ValueError("Unknown managed browser tool")
    return replace(
        spec,
        owner_only=True,
        plan_access=PlanAccess.READ_ONLY
        if tool_name
        in {
            "browser_tabs",
            "browser_inspect",
            "browser_screenshot",
            "browser_observe",
        }
        else PlanAccess.DENY,
        sandbox=SandboxToolDescriptor.custom(kind="browser"),
    )
