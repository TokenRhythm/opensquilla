"""Shared canonicalization for provider-emitted tool arguments."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

_TOOL_ARGUMENT_ALIASES: dict[str, dict[str, str]] = {
    "edit_file": {
        "file_path": "path",
        "filePath": "path",
        "old_string": "old_text",
        "oldString": "old_text",
        "oldText": "old_text",
        "new_string": "new_text",
        "newString": "new_text",
        "newText": "new_text",
    },
    "read_file": {
        "file_path": "path",
        "filePath": "path",
    },
    "write_file": {
        "file_path": "path",
        "filePath": "path",
    },
}


@dataclass(frozen=True)
class ToolArgumentAliasConflict:
    """A conflicting alias/canonical value pair for a tool argument."""

    alias: str
    canonical: str
    conflicting_with: str


@dataclass(frozen=True)
class ToolArgumentNormalizationResult:
    """Result of canonicalizing common model-emitted argument aliases."""

    arguments: dict[str, Any]
    aliases_applied: list[dict[str, str]] = field(default_factory=list)
    conflicts: list[ToolArgumentAliasConflict] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.aliases_applied)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


def canonicalize_tool_arguments(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> ToolArgumentNormalizationResult:
    """Map known tool argument aliases to canonical names without guessing values."""

    aliases = _TOOL_ARGUMENT_ALIASES.get(tool_name)
    normalized = dict(arguments)
    if not aliases:
        return ToolArgumentNormalizationResult(arguments=normalized)

    aliases_by_canonical: dict[str, list[str]] = {}
    for alias, canonical in aliases.items():
        if alias in normalized:
            aliases_by_canonical.setdefault(canonical, []).append(alias)

    aliases_applied: list[dict[str, str]] = []
    conflicts: list[ToolArgumentAliasConflict] = []
    for canonical, present_aliases in aliases_by_canonical.items():
        canonical_present = canonical in normalized
        if canonical_present:
            canonical_value = normalized[canonical]
            for alias in present_aliases:
                if normalized[alias] != canonical_value:
                    conflicts.append(
                        ToolArgumentAliasConflict(
                            alias=alias,
                            canonical=canonical,
                            conflicting_with=canonical,
                        )
                    )
                    continue
                normalized.pop(alias)
                aliases_applied.append({"alias": alias, "canonical": canonical})
            continue

        selected_alias = present_aliases[0]
        selected_value = normalized[selected_alias]
        local_conflicts: list[ToolArgumentAliasConflict] = []
        for alias in present_aliases[1:]:
            if normalized[alias] != selected_value:
                local_conflicts.append(
                    ToolArgumentAliasConflict(
                        alias=alias,
                        canonical=canonical,
                        conflicting_with=selected_alias,
                    )
                )

        if local_conflicts:
            conflicts.extend(local_conflicts)
            continue
        normalized[canonical] = selected_value
        for alias in present_aliases:
            normalized.pop(alias)
            aliases_applied.append({"alias": alias, "canonical": canonical})

    return ToolArgumentNormalizationResult(
        arguments=normalized,
        aliases_applied=aliases_applied,
        conflicts=conflicts,
    )


def format_alias_conflicts(conflicts: list[ToolArgumentAliasConflict]) -> list[str]:
    """Render alias conflicts without leaking argument values."""

    return [
        (
            f"{conflict.alias} conflicts with {conflict.conflicting_with} "
            f"for canonical argument {conflict.canonical}"
        )
        for conflict in conflicts
    ]
