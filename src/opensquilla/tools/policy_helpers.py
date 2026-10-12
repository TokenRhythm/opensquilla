"""Tool policy resolution for runtime tool visibility and dispatch."""

from __future__ import annotations

from dataclasses import replace

from opensquilla.tools import policy_config, policy_runtime
from opensquilla.tools.types import ToolContext

ToolPolicy = policy_config.ToolPolicy
ToolSurfaceCapabilities = policy_runtime.ToolSurfaceCapabilities
detect_runtime_tool_surface_capabilities = (
    policy_runtime.detect_runtime_tool_surface_capabilities
)
private_memory_read_tool_denied = policy_runtime.private_memory_read_tool_denied
private_memory_read_tools_blocked = policy_runtime.private_memory_read_tools_blocked
resolve_runtime_tool_surface = policy_runtime.resolve_runtime_tool_surface
tool_surface_capabilities_from_runtime = (
    policy_runtime.tool_surface_capabilities_from_runtime
)


def apply_tool_policy(
    ctx: ToolContext,
    *,
    available_tools: list[str],
    global_policy: ToolPolicy | None = None,
    agent_policy: ToolPolicy | None = None,
    default_channel_policy: ToolPolicy | None = None,
    channel_policy: ToolPolicy | None = None,
) -> ToolContext:
    """Return a ``ToolContext`` with resolved allow/deny sets.

    Global and agent policy establish the base allowlist and hard denies.
    Agent profile overrides global profile. Channel/default/sender layers can
    further restrict or add tools, but global/agent denies still win.
    """

    available = frozenset(available_tools)
    allowed_tools = set(ctx.allowed_tools) if ctx.allowed_tools is not None else None
    denied_tools = set(ctx.denied_tools)

    allowed_tools, denied_tools = policy_config.apply_base_policy(
        allowed_tools,
        denied_tools,
        global_policy,
        available,
    )
    allowed_tools, denied_tools = policy_config.apply_base_policy(
        allowed_tools,
        denied_tools,
        agent_policy,
        available,
        profile_overrides=True,
    )
    hard_denied = set(denied_tools)

    channel_denied: set[str] = set()
    allowed_tools, channel_denied = policy_config.apply_channel_layer(
        allowed_tools,
        channel_denied,
        default_channel_policy,
        available,
    )
    allowed_tools, channel_denied = policy_config.apply_sender_layer(
        allowed_tools,
        channel_denied,
        policy_config.sender_policy(default_channel_policy, ctx.sender_id),
        available,
    )
    allowed_tools, channel_denied = policy_config.apply_channel_layer(
        allowed_tools,
        channel_denied,
        channel_policy,
        available,
    )
    allowed_tools, channel_denied = policy_config.apply_sender_layer(
        allowed_tools,
        channel_denied,
        policy_config.sender_policy(channel_policy, ctx.sender_id),
        available,
    )

    denied_tools = hard_denied | channel_denied
    if allowed_tools is not None:
        allowed_tools -= denied_tools

    return replace(
        ctx,
        allowed_tools=allowed_tools,
        denied_tools=denied_tools,
        workspace_write_deny_globs=_merged_workspace_write_deny_globs(
            ctx,
            global_policy,
            agent_policy,
            default_channel_policy,
            policy_config.sender_policy(default_channel_policy, ctx.sender_id),
            channel_policy,
            policy_config.sender_policy(channel_policy, ctx.sender_id),
        ),
        file_edit_requires_fresh_read=_merged_file_edit_requires_fresh_read(
            ctx,
            global_policy,
            agent_policy,
            default_channel_policy,
            policy_config.sender_policy(default_channel_policy, ctx.sender_id),
            channel_policy,
            policy_config.sender_policy(channel_policy, ctx.sender_id),
        ),
        file_edit_flexible_recovery=_merged_file_edit_flexible_recovery(
            ctx,
            global_policy,
            agent_policy,
            default_channel_policy,
            policy_config.sender_policy(default_channel_policy, ctx.sender_id),
            channel_policy,
            policy_config.sender_policy(channel_policy, ctx.sender_id),
        ),
    )


def apply_tool_policy_layer(
    ctx: ToolContext,
    policy: object,
    *,
    available_tools: list[str] | set[str] | frozenset[str],
    hard_denied: set[str] | frozenset[str] | None = None,
) -> ToolContext:
    """Apply one declarative policy layer to an existing context.

    This is used for persisted cron job policy carried through route metadata.
    It intentionally keeps the caller's current allowlist unless the policy
    selects a narrower named profile, and reapplies ``hard_denied`` at the end
    so lower layers cannot revive denied tools.
    """

    parsed = policy_config.policy_from_config(policy)
    if parsed is None:
        return ctx
    allowed_tools = set(ctx.allowed_tools) if ctx.allowed_tools is not None else None
    denied_tools = set(ctx.denied_tools)
    allowed_tools, denied_tools = policy_config.apply_base_policy(
        allowed_tools,
        denied_tools,
        parsed,
        frozenset(available_tools),
        profile_overrides=False,
    )
    if hard_denied:
        denied_tools |= set(hard_denied)
    if allowed_tools is not None:
        allowed_tools -= denied_tools
    return replace(
        ctx,
        allowed_tools=allowed_tools,
        denied_tools=denied_tools,
        workspace_write_deny_globs=_merged_workspace_write_deny_globs(ctx, parsed),
        file_edit_requires_fresh_read=_merged_file_edit_requires_fresh_read(ctx, parsed),
        file_edit_flexible_recovery=_merged_file_edit_flexible_recovery(ctx, parsed),
    )


def _merged_workspace_write_deny_globs(
    ctx: ToolContext,
    *policies: ToolPolicy | None,
) -> list[str]:
    merged: list[str] = list(ctx.workspace_write_deny_globs)
    seen = set(merged)
    for policy in policies:
        if policy is None:
            continue
        for pattern in policy.workspace_write_deny_globs:
            if pattern in seen:
                continue
            merged.append(pattern)
            seen.add(pattern)
    return merged


def _merged_file_edit_requires_fresh_read(
    ctx: ToolContext,
    *policies: ToolPolicy | None,
) -> bool:
    enabled = ctx.file_edit_requires_fresh_read
    for policy in policies:
        if policy is None:
            continue
        if policy.profile and policy.profile.strip().lower() == "coding":
            enabled = True
        if policy.file_edit_requires_fresh_read is not None:
            enabled = policy.file_edit_requires_fresh_read
    return enabled


def _merged_file_edit_flexible_recovery(
    ctx: ToolContext,
    *policies: ToolPolicy | None,
) -> bool:
    enabled = ctx.file_edit_flexible_recovery
    for policy in policies:
        if policy is None:
            continue
        if policy.file_edit_flexible_recovery is not None:
            enabled = policy.file_edit_flexible_recovery
    return enabled


def apply_tool_policy_from_config(
    ctx: ToolContext,
    *,
    available_tools: list[str],
    config: object | None,
) -> ToolContext:
    """Apply config-shaped tool policy to a context.

    Supported config shape intentionally mirrors the documented policy concepts:
    ``config.tools``, ``config.agents[agent_id].tools`` or
    ``config.agents.list[].tools``, and channel entries such as
    ``config.channels.telegram.groups["room"].tools`` with optional
    ``toolsBySender``.
    """

    if config is None:
        return ctx
    default_channel_policy, channel_policy = policy_config.channel_entry_policy_from_config(
        config, ctx
    )
    return apply_tool_policy(
        ctx,
        available_tools=available_tools,
        global_policy=policy_config.policy_from_config(policy_config.get_field(config, "tools")),
        agent_policy=policy_config.agent_policy_from_config(config, ctx.agent_id),
        default_channel_policy=default_channel_policy,
        channel_policy=channel_policy,
    )
