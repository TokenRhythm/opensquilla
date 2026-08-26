"""Declarative presentation rules for tool calls.

The resolver deliberately stops at two levels: exact tool exceptions and
category defaults.  Unknown/plugin tools may declare a category on ``ToolSpec``;
otherwise sandbox semantics and, finally, parameter shape provide a bounded
fallback.  This module describes presentation only.  It must never alter the
authoritative arguments used for validation or execution.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from opensquilla.tools.types import ToolPresentationCategory, ToolSpec

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
    "file_read": _CategoryRule(("paths", "path", "file_path", "workdir"), "primary", "boundary"),
    "network_read": _CategoryRule(("urls", "url"), "primary", "boundary"),
    "command": _CategoryRule(("command", "code"), "all", "default"),
    "subagent": _CategoryRule(("task",), "all", "default"),
    "mutation": _CategoryRule(("path", "paths", "file_path", "files"), "all", "default"),
    "generic": _CategoryRule((), "all", "default"),
}

# Category membership is grouped instead of repeating a complete rule per tool.
# These are semantic exceptions for built-ins whose sandbox kind is broader than
# their presentation behavior.  Plugins should prefer ToolSpec.presentation_category.
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
    # These names are important negative exceptions: their parameter names
    # resemble another category, but their semantics do not.
    "generic": frozenset({"cron", "document_browser_reload", "retrieve_tool_result"}),
}

_TOOL_CATEGORY: dict[str, ToolPresentationCategory] = {
    tool_name: category
    for category, tool_names in _TOOLS_BY_CATEGORY.items()
    for tool_name in tool_names
}

# Only genuine deviations from a category's normal key belong here.
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
    # Web-search queries are invocation parameters, not resource targets. The
    # UI lists the result URLs separately, so publishing the query would put a
    # parameter back into the read-only activity surface.
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


def _parameter_names(spec: ToolSpec) -> tuple[str, ...]:
    parameters = spec.parameters
    if parameters.get("type") == "object" and isinstance(parameters.get("properties"), dict):
        parameters = parameters["properties"]
    return tuple(str(name) for name in parameters)


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


def resolve_tool_presentation(spec: ToolSpec) -> ToolPresentationRule:
    """Resolve one tool using exact override → category → shape fallback."""

    tool_name = str(spec.name or "").strip().lower()
    parameter_names = _parameter_names(spec)

    # Level 1: a named built-in may be a semantic exception to its broad
    # sandbox descriptor or category defaults.
    category = _TOOL_CATEGORY.get(tool_name)

    # Level 2: explicit registration metadata wins over all inference for
    # tools without a named exception.
    if category is None and spec.presentation_category in _VALID_CATEGORIES:
        category = cast(ToolPresentationCategory, spec.presentation_category)

    # Existing registrations already carry operation semantics.  Reuse those
    # before consulting field names, which are only a compatibility fallback.
    if category is None:
        category = _category_from_sandbox_kind(spec.sandbox.kind)
    if category is None:
        category = _category_from_parameter_shape(parameter_names)

    base = _CATEGORY_RULES[category]
    primary_arguments = _PRIMARY_ARGUMENT_EXCEPTIONS.get(tool_name)
    if primary_arguments is not None:
        available = set(parameter_names)
        primary_arguments = tuple(key for key in primary_arguments if key in available)
    else:
        available = set(parameter_names)
        primary_arguments = tuple(
            key for key in base.primary_argument_candidates if key in available
        )
    return ToolPresentationRule(
        category=category,
        primary_arguments=primary_arguments,
        argument_display=base.argument_display,
        lifecycle_display=base.lifecycle_display,
    )


def project_tool_arguments(
    rule: ToolPresentationRule,
    arguments: dict[str, Any],
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
    "ToolPresentationRule",
    "project_tool_arguments",
    "project_tool_arguments_payload",
    "resolve_tool_presentation",
]
