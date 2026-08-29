"""Pure presentation policy shared by tool and transcript boundaries.

This module accepts primitive registration fields so lower-level consumers do
not need to import the tool registry package.  It describes public rendering
only and never changes validation, authorization, or execution arguments.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

ToolPresentationCategory = Literal[
    "search",
    "file_read",
    "network_read",
    "command",
    "subagent",
    "mutation",
    "generic",
]
ToolArgumentDisplay = Literal["primary", "all"]
ToolLifecycleDisplay = Literal["boundary", "default"]


@dataclass(frozen=True, slots=True)
class ToolPresentationRule:
    """Resolved, UI-facing policy for one registered tool."""

    category: ToolPresentationCategory
    primary_arguments: tuple[str, ...]
    argument_display: ToolArgumentDisplay
    lifecycle_display: ToolLifecycleDisplay

    def to_payload(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "primaryArguments": list(self.primary_arguments),
            "argumentDisplay": self.argument_display,
            "lifecycleDisplay": self.lifecycle_display,
        }


@dataclass(frozen=True, slots=True)
class _CategoryRule:
    primary_argument_candidates: tuple[str, ...]
    argument_display: ToolArgumentDisplay
    lifecycle_display: ToolLifecycleDisplay


_CATEGORY_RULES: dict[ToolPresentationCategory, _CategoryRule] = {
    "search": _CategoryRule(("queries", "query", "pattern"), "primary", "boundary"),
    "file_read": _CategoryRule(
        ("paths", "path", "file_path", "workdir"), "primary", "boundary"
    ),
    "network_read": _CategoryRule(("urls", "url"), "primary", "boundary"),
    "command": _CategoryRule(("command", "code"), "all", "default"),
    "subagent": _CategoryRule(("task",), "all", "default"),
    "mutation": _CategoryRule(
        ("path", "paths", "file_path", "files"), "all", "default"
    ),
    "generic": _CategoryRule((), "all", "default"),
}

# Exact semantic categories stay grouped so the resolver remains a shallow
# exact override -> declared category -> bounded fallback decision.
_TOOLS_BY_CATEGORY: dict[ToolPresentationCategory, frozenset[str]] = {
    "search": frozenset(
        {
            "glob_search",
            "grep_search",
            "memory_search",
            "session_search",
            "source_symbols",
            "voice_search",
            "web_discover",
            "web_search",
        }
    ),
    "file_read": frozenset(
        {
            "document_browser_inspect",
            "document_browser_screenshot",
            "document_inspect",
            "document_locate",
            "document_read",
            "skill_view",
        }
    ),
    "network_read": frozenset(),
    "command": frozenset({"process"}),
    "subagent": frozenset({"sessions_spawn", "subagents"}),
    "mutation": frozenset(
        {
            "canvas",
            "document_browser_act",
            "document_finish",
            "install_skill_deps",
            "memory_delete",
            "skill_install_community",
            "skill_create",
            "skill_delete",
            "skill_edit",
        }
    ),
    "generic": frozenset(
        {"cron", "document_browser_reload", "retrieve_tool_result"}
    ),
}

_TOOL_CATEGORY: dict[str, ToolPresentationCategory] = {
    tool_name: category
    for category, tool_names in _TOOLS_BY_CATEGORY.items()
    for tool_name in tool_names
}

_PRIMARY_ARGUMENT_EXCEPTIONS: dict[str, tuple[str, ...]] = {
    "apply_patch": ("path", "patch"),
    "canvas": ("action", "node_id"),
    "create_csv": ("name",),
    "create_pdf_report": ("name", "title"),
    "create_pptx": ("name",),
    "create_xlsx": ("name",),
    "document_apply": ("mutations",),
    "document_browser_act": ("action", "anchor"),
    "document_finish": ("decision",),
    "document_patch": ("edits",),
    "execute_code": ("code",),
    "glob_search": ("pattern", "path"),
    "grep_search": ("pattern", "path"),
    "process": ("action", "session_id", "sessionId"),
    "subagents": ("action", "session_key"),
    "skill_view": ("name", "file_path"),
    "voice_search": ("search",),
    # Queries are invocation parameters; the UI lists search result URLs as
    # the public resource targets instead.
    "web_discover": (),
    "web_search": (),
}

_VALID_CATEGORIES = frozenset(_CATEGORY_RULES)
_MUTATION_ARGUMENTS = frozenset(
    {
        "content",
        "edits",
        "lyrics",
        "mutations",
        "new_text",
        "patch",
        "replacement",
        "rows",
        "sheets",
        "skill_md",
        "slides",
    }
)


def _category_from_sandbox_kind(kind: str) -> ToolPresentationCategory | None:
    normalized = str(kind or "").strip().lower()
    if not normalized:
        return None
    if normalized.startswith(("fs.write", "fs.edit", "patch.")):
        return "mutation"
    if normalized.startswith(
        ("document.apply", "document.patch", "artifact.create", "artifact.publish")
    ):
        return "mutation"
    if normalized.startswith("git.write"):
        return "mutation"
    if normalized.startswith(("network.", "web.")):
        return "network_read"
    if normalized.startswith(("fs.read", "git.read", "media.read")):
        return "file_read"
    if normalized.startswith(("shell.", "code.")):
        return "command"
    return None


def _category_from_parameter_shape(
    parameter_names: tuple[str, ...],
) -> ToolPresentationCategory:
    names = set(parameter_names)
    if "command" in names or "code" in names:
        return "command"
    if "task" in names:
        return "subagent"
    if "url" in names or "urls" in names:
        return "network_read"
    if names & _MUTATION_ARGUMENTS:
        return "mutation"
    if "queries" in names or "query" in names or "pattern" in names:
        return "search"
    if names & {"paths", "path", "file_path", "workdir"}:
        return "file_read"
    return "generic"


def resolve_tool_presentation_fields(
    *,
    name: str,
    parameter_names: tuple[str, ...],
    sandbox_kind: str = "",
    presentation_category: str | None = None,
) -> ToolPresentationRule:
    """Resolve exact override -> category -> shape from primitive fields."""

    tool_name = str(name or "").strip().lower()
    category = _TOOL_CATEGORY.get(tool_name)
    if category is None and presentation_category in _VALID_CATEGORIES:
        category = cast(ToolPresentationCategory, presentation_category)
    if category is None:
        category = _category_from_sandbox_kind(sandbox_kind)
    if category is None:
        category = _category_from_parameter_shape(parameter_names)

    base = _CATEGORY_RULES[category]
    available = set(parameter_names)
    primary_arguments = _PRIMARY_ARGUMENT_EXCEPTIONS.get(tool_name)
    if primary_arguments is None:
        primary_arguments = base.primary_argument_candidates
    primary_arguments = tuple(key for key in primary_arguments if key in available)
    return ToolPresentationRule(
        category=category,
        primary_arguments=primary_arguments,
        argument_display=base.argument_display,
        lifecycle_display=base.lifecycle_display,
    )


def project_tool_arguments(
    rule: ToolPresentationRule,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the public argument view without changing execution input."""

    if rule.argument_display == "all":
        return dict(arguments)
    return {
        key: arguments[key]
        for key in rule.primary_arguments
        if key in arguments
    }


def project_tool_arguments_payload(
    presentation: Mapping[str, Any] | None,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Project arguments from serialized rule metadata at a public boundary."""

    if presentation is None:
        return dict(arguments)
    argument_display = presentation.get("argumentDisplay")
    if argument_display == "all":
        return dict(arguments)
    if argument_display != "primary":
        return {}
    primary = presentation.get("primaryArguments")
    if not isinstance(primary, list):
        return {}
    return {
        key: arguments[key]
        for key in primary
        if isinstance(key, str) and key in arguments
    }


__all__ = [
    "ToolPresentationCategory",
    "ToolPresentationRule",
    "project_tool_arguments",
    "project_tool_arguments_payload",
    "resolve_tool_presentation_fields",
]
