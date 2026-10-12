"""Declarative tool policy config and selector helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from opensquilla.tools.types import ToolContext

_TOOL_GROUPS: Mapping[str, frozenset[str]] = {
    "group:runtime": frozenset({"exec_command", "background_process"}),
    "group:fs": frozenset(
        {
            "read_file",
            "write_file",
            "edit_file",
            "apply_patch",
            "list_dir",
            "glob_search",
            "grep_search",
        }
    ),
    "group:sessions": frozenset(
        {"sessions_list", "sessions_history", "sessions_send", "sessions_spawn", "session_status"}
    ),
    "group:memory": frozenset({"memory_search", "memory_get"}),
    "group:web": frozenset({"web_search", "web_discover", "web_fetch", "http_request"}),
    "group:messaging": frozenset({"message"}),
    "channel:chat": frozenset(
        {
            "message",
            "sessions_list",
            "sessions_history",
            "sessions_send",
            "session_status",
        }
    ),
    "channel:media": frozenset(
        {
            "create_csv",
            "create_pdf_report",
            "create_pptx",
            "create_xlsx",
            "image",
            "image_generate",
            "audio_provider_capabilities",
            "dubbing_download",
            "dubbing_generate",
            "dubbing_status",
            "music_generate",
            "pdf",
            "publish_artifact",
            "song_generate",
            "tts",
            "voice_clone",
            "voice_convert",
            "voice_search",
        }
    ),
    "channel:doc": frozenset(
        {
            "create_pdf_report",
            "web_discover",
            "web_fetch",
            "web_search",
        }
    ),
    "channel:wiki": frozenset(
        {
            "web_discover",
            "web_fetch",
            "web_search",
        }
    ),
    "channel:drive": frozenset(
        {
            "create_csv",
            "create_pdf_report",
            "create_pptx",
            "create_xlsx",
        }
    ),
    "channel:scopes": frozenset(),
    "channel:perm": frozenset(),
    # Trusted host/gateway tools intentionally do not imply OS sandbox
    # execution. They remain addressable for explicit allow/deny policy so the
    # sandboxed-agent tool surface is not confused with operator-owned host
    # mutation paths.
    "group:trusted_host": frozenset(
        {
            "install_skill_deps",
            "skill_install_community",
            "skill_create",
            "skill_edit",
            "skill_delete",
        }
    ),
}

_REPO_CODING_SOURCE_EDIT_TOOLS: frozenset[str] = frozenset(
    {
        "read_source",
        "edit_source",
        "read_file",
        "grep_search",
        "glob_search",
        "list_dir",
        "git_status",
        "git_diff",
        "retrieve_tool_result",
        "exec_command",
    }
)

_REPO_CODING_SOURCE_EDIT_STRICT_TOOLS: frozenset[str] = frozenset(
    {
        "read_source",
        "edit_source",
        "grep_search",
        "glob_search",
        "git_status",
        "git_diff",
        "retrieve_tool_result",
        "exec_command",
    }
)

_REPO_CODING_SOURCE_EDIT_V2_TOOLS: frozenset[str] = frozenset(
    {
        "read_source",
        "edit_source",
        "source_symbols",
        "grep_search",
        "glob_search",
        "git_status",
        "git_diff",
        "retrieve_tool_result",
        "exec_command",
    }
)

_REPO_CODING_SOURCE_EDIT_BALANCED_TOOLS: frozenset[str] = frozenset(
    {
        "read_source",
        "edit_source",
        "create_source",
        "write_scratch",
        "source_symbols",
        "read_file",
        "grep_search",
        "glob_search",
        "list_dir",
        "git_status",
        "git_diff",
        "retrieve_tool_result",
        "exec_command",
    }
)

_REPO_CODING_SOURCE_EDIT_PATCH_FALLBACK_TOOLS: frozenset[str] = (
    _REPO_CODING_SOURCE_EDIT_BALANCED_TOOLS | frozenset({"apply_patch"})
)

_REPO_CODING_SCAFFOLD_EDIT_TOOLS: frozenset[str] = frozenset(
    {
        "exec_command",
        "read_file",
        "edit_file",
        "write_file",
        "glob_search",
        "grep_search",
        "list_dir",
        "git_status",
        "git_diff",
        "retrieve_tool_result",
    }
)

_REPO_CODING_SCAFFOLD_PATCH_TOOLS: frozenset[str] = (
    _REPO_CODING_SCAFFOLD_EDIT_TOOLS | frozenset({"apply_patch"})
)

_TOOL_PROFILES: Mapping[str, frozenset[str] | None] = {
    "full": None,
    "minimal": frozenset({"session_status"}),
    "memory_only": _TOOL_GROUPS["group:memory"],
    "coding": (
        _TOOL_GROUPS["group:fs"]
        | _TOOL_GROUPS["group:runtime"]
        | _TOOL_GROUPS["group:sessions"]
        | _TOOL_GROUPS["group:memory"]
    ),
    "repo_coding_source_edit": _REPO_CODING_SOURCE_EDIT_TOOLS,
    "repo_coding_source_edit_strict": _REPO_CODING_SOURCE_EDIT_STRICT_TOOLS,
    "repo_coding_source_edit_v2": _REPO_CODING_SOURCE_EDIT_V2_TOOLS,
    "repo_coding_source_edit_balanced": _REPO_CODING_SOURCE_EDIT_BALANCED_TOOLS,
    "repo_coding_source_edit_patch_fallback": _REPO_CODING_SOURCE_EDIT_PATCH_FALLBACK_TOOLS,
    "repo_coding_scaffold_edit": _REPO_CODING_SCAFFOLD_EDIT_TOOLS,
    "repo_coding_scaffold_patch": _REPO_CODING_SCAFFOLD_PATCH_TOOLS,
    "messaging": _TOOL_GROUPS["group:messaging"]
    | frozenset({"sessions_list", "sessions_history", "sessions_send", "session_status"}),
}
_SENDER_SCOPED_TOOL_GROUPS: frozenset[str] = frozenset({"channel:perm"})
_SENDER_SCOPED_TOOL_NAMES: frozenset[str] = _TOOL_GROUPS["channel:perm"]


@dataclass(frozen=True)
class ToolPolicy:
    """Declarative tool policy layer.

    ``profile`` sets the base allowlist; ``allow`` and ``also_allow`` add
    selectors; ``deny`` removes selectors. ``workspace_write_deny_globs`` can
    further block writes to matching workspace-relative paths. Selectors can be
    exact tool names, ``group:*`` names, ``*``, or fnmatch-style patterns.
    """

    profile: str | None = None
    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()
    also_allow: frozenset[str] = frozenset()
    workspace_write_deny_globs: frozenset[str] = frozenset()
    file_edit_requires_fresh_read: bool | None = None
    file_edit_flexible_recovery: bool | None = None
    by_sender: Mapping[str, ToolPolicy] = field(default_factory=dict)


# Coding mode (operator toggle): the in-session write tools that let the
# agent hand-edit a repository. When coding mode is ON these are denied so
# every code change is forced through the code-task plugin instead. Shell
# (exec_command/background_process) is intentionally kept so the agent can
# still LAUNCH code-task.
CODING_MODE_DENIED_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "apply_patch",
        "execute_code",
        "git_commit",
        "create_csv",
        "create_pdf_report",
        "create_pptx",
        "create_xlsx",
    }
)


def coding_mode_denied_tools(coding_mode: bool) -> frozenset[str]:
    """Tools to deny while the coding-mode toggle is on (empty when off)."""
    return CODING_MODE_DENIED_TOOLS if coding_mode else frozenset()


def expand_selectors(selectors: frozenset[str], available_tools: frozenset[str]) -> set[str]:
    expanded: set[str] = set()
    for selector in selectors:
        item = selector.strip()
        if not item:
            continue
        if item == "*":
            expanded.update(available_tools)
            continue
        if item in _TOOL_GROUPS:
            expanded.update(_TOOL_GROUPS[item] & available_tools)
            continue
        if any(ch in item for ch in "*?[]"):
            expanded.update(tool for tool in available_tools if fnmatchcase(tool, item))
            continue
        if item in available_tools:
            expanded.add(item)
    return expanded


def profile_allowlist(profile: str | None, available_tools: frozenset[str]) -> set[str] | None:
    if not profile:
        return None
    key = profile.strip().lower()
    if key not in _TOOL_PROFILES:
        raise ValueError(f"unknown tool profile: {profile}")
    expanded = _TOOL_PROFILES[key]
    if expanded is None:
        return None
    return set(expanded & available_tools)


def add_allowed(
    allowed_tools: set[str] | None,
    additions: set[str],
) -> set[str] | None:
    if allowed_tools is None:
        return None
    return allowed_tools | additions


def apply_base_policy(
    allowed_tools: set[str] | None,
    denied_tools: set[str],
    policy: ToolPolicy | None,
    available_tools: frozenset[str],
    *,
    profile_overrides: bool = False,
) -> tuple[set[str] | None, set[str]]:
    if policy is None:
        return allowed_tools, denied_tools

    profile_allowed = profile_allowlist(policy.profile, available_tools)
    if profile_allowed is not None or (profile_overrides and policy.profile == "full"):
        allowed_tools = profile_allowed

    allowed_tools = add_allowed(
        allowed_tools,
        expand_selectors(policy.allow | policy.also_allow, available_tools),
    )
    denied_tools = denied_tools | expand_selectors(policy.deny, available_tools)
    if allowed_tools is not None:
        allowed_tools -= denied_tools
    return allowed_tools, denied_tools


def matches_sender(selector: str, sender_id: str | None) -> bool:
    normalized = selector.strip()
    if normalized == "*":
        return True
    if not sender_id:
        return False
    if ":" in normalized:
        key, value = normalized.split(":", 1)
        if key.strip().lower() == "id":
            return value == sender_id
        return False
    return normalized == sender_id


def get_field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def string_set(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(str(item) for item in value if str(item).strip())
    return frozenset()


def optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _file_edit_requires_fresh_read_from_config(value: object) -> bool | None:
    return optional_bool(
        get_field(
            value,
            "fileEditRequiresFreshRead",
            get_field(value, "file_edit_requires_fresh_read"),
        )
    )


def _file_edit_flexible_recovery_from_config(value: object) -> bool | None:
    return optional_bool(
        get_field(
            value,
            "fileEditFlexibleRecovery",
            get_field(value, "file_edit_flexible_recovery"),
        )
    )


def policy_from_config(value: object) -> ToolPolicy | None:
    if value is None:
        return None
    if isinstance(value, ToolPolicy):
        return value

    tools_value = get_field(value, "tools")
    sender_value = get_field(value, "toolsBySender", get_field(value, "tools_by_sender"))
    if tools_value is not None:
        base = policy_from_config(tools_value) or ToolPolicy()
        wrapper_by_sender = sender_policies_from_config(sender_value)
        fresh_read = _file_edit_requires_fresh_read_from_config(value)
        flexible_recovery = _file_edit_flexible_recovery_from_config(value)
        return ToolPolicy(
            profile=base.profile,
            allow=base.allow,
            deny=base.deny,
            also_allow=base.also_allow,
            workspace_write_deny_globs=base.workspace_write_deny_globs
            | string_set(
                get_field(
                    value,
                    "workspaceWriteDenyGlobs",
                    get_field(value, "workspace_write_deny_globs"),
                )
            ),
            file_edit_requires_fresh_read=(
                fresh_read if fresh_read is not None else base.file_edit_requires_fresh_read
            ),
            file_edit_flexible_recovery=(
                flexible_recovery
                if flexible_recovery is not None
                else base.file_edit_flexible_recovery
            ),
            by_sender={**base.by_sender, **wrapper_by_sender},
        )

    profile = get_field(value, "profile")
    return ToolPolicy(
        profile=str(profile) if profile is not None else None,
        allow=string_set(get_field(value, "allow")),
        deny=string_set(get_field(value, "deny")),
        also_allow=string_set(get_field(value, "alsoAllow", get_field(value, "also_allow"))),
        workspace_write_deny_globs=string_set(
            get_field(
                value,
                "workspaceWriteDenyGlobs",
                get_field(value, "workspace_write_deny_globs"),
            )
        ),
        file_edit_requires_fresh_read=_file_edit_requires_fresh_read_from_config(value),
        file_edit_flexible_recovery=_file_edit_flexible_recovery_from_config(value),
        by_sender=sender_policies_from_config(
            sender_value
            if sender_value is not None
            else get_field(value, "by_sender", get_field(value, "bySender"))
        ),
    )


def sender_policies_from_config(value: object) -> Mapping[str, ToolPolicy]:
    if not isinstance(value, Mapping):
        return {}
    policies: dict[str, ToolPolicy] = {}
    for selector, policy_value in value.items():
        policy = policy_from_config(policy_value)
        if policy is not None:
            policies[str(selector)] = policy
    return policies


def sender_policy(policy: ToolPolicy | None, sender_id: str | None) -> ToolPolicy | None:
    if policy is None:
        return None
    for selector, candidate in policy.by_sender.items():
        if matches_sender(selector, sender_id):
            return candidate
    return None


def remove_denied_from_allowed(
    allowed_tools: set[str] | None,
    denied_tools: set[str],
) -> set[str] | None:
    if allowed_tools is not None:
        allowed_tools -= denied_tools
    return allowed_tools


def agent_policy_from_config(config: object, agent_id: str) -> ToolPolicy | None:
    agents = get_field(config, "agents")
    if isinstance(agents, Mapping):
        return policy_from_config(get_field(agents.get(agent_id), "tools"))

    entries = agents if isinstance(agents, list | tuple) else get_field(agents, "list", [])
    if isinstance(entries, list | tuple):
        for entry in entries:
            if get_field(entry, "id") == agent_id:
                return policy_from_config(get_field(entry, "tools"))
    return None


def channel_entry_policy_from_config(
    config: object,
    ctx: ToolContext,
) -> tuple[
    ToolPolicy | None,
    ToolPolicy | None,
]:
    if not ctx.channel_kind:
        return None, None

    channels = get_field(config, "channels")
    channel_cfg = get_field(channels, ctx.channel_kind)
    if channel_cfg is None:
        return None, None

    entries: object = None
    for field_name in ("groups", "channels", "rooms"):
        entries = get_field(channel_cfg, field_name)
        if isinstance(entries, Mapping):
            break
    if not isinstance(entries, Mapping):
        return None, None

    default_policy = policy_from_config(entries.get("*"))
    specific_policy = policy_from_config(entries.get(ctx.channel_id or ""))
    return default_policy, specific_policy


def apply_channel_layer(
    allowed_tools: set[str] | None,
    channel_denied: set[str],
    policy: ToolPolicy | None,
    available_tools: frozenset[str],
) -> tuple[set[str] | None, set[str]]:
    if policy is None:
        return allowed_tools, channel_denied
    profile_allowed = profile_allowlist(policy.profile, available_tools)
    if profile_allowed is not None:
        allowed_tools = profile_allowed
    channel_selectors = (
        (policy.allow | policy.also_allow)
        - _SENDER_SCOPED_TOOL_GROUPS
        - _SENDER_SCOPED_TOOL_NAMES
    )
    allowed_tools = add_allowed(
        allowed_tools,
        expand_selectors(channel_selectors, available_tools),
    )
    channel_denied |= expand_selectors(policy.deny, available_tools)
    return allowed_tools, channel_denied


def apply_sender_layer(
    allowed_tools: set[str] | None,
    channel_denied: set[str],
    policy: ToolPolicy | None,
    available_tools: frozenset[str],
) -> tuple[set[str] | None, set[str]]:
    if policy is None:
        return allowed_tools, channel_denied
    also_allowed = expand_selectors(policy.also_allow, available_tools)
    channel_denied -= also_allowed
    allowed_tools = add_allowed(allowed_tools, expand_selectors(policy.allow, available_tools))
    allowed_tools = add_allowed(allowed_tools, also_allowed)
    channel_denied |= expand_selectors(policy.deny, available_tools)
    return allowed_tools, channel_denied
