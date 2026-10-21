"""Bounded, turn-local project instruction snapshots and /init input."""

from __future__ import annotations

import os
from collections.abc import Set
from dataclasses import dataclass, field
from pathlib import Path

from opensquilla.safety.injection_guard import scan_for_injection, wrap_untrusted

from .prompt import _make_env

SOURCE_TOOLS = frozenset({"read_source", "create_source", "edit_source"})
_TEMPLATES = Path(__file__).parent / "templates"


@dataclass(frozen=True)
class ProjectRule:
    path: str
    scope: str
    read_status: str
    content: str = field(default="", repr=False)


@dataclass(frozen=True)
class ProjectRulesSnapshot:
    workspace_root: str
    rules: tuple[ProjectRule, ...] = field(default=(), repr=False)

    def includes_file(self, path: Path) -> bool:
        # The project root owns its live AGENTS candidate even when it was
        # deleted or replaced by the legacy name after bootstrap was cached.
        try:
            if path.parent.samefile(self.workspace_root):
                return True
        except OSError:
            if path.parent.absolute() == Path(self.workspace_root).absolute():
                return True
        for rule in self.rules:
            try:
                if path.samefile(rule.path):
                    return True
            except OSError:
                if path.absolute() == Path(rule.path).absolute():
                    return True
        return False

    def render(self) -> str:
        if not self.rules:
            return ""
        return _make_env().get_template("project_rules.md.j2").render(
            workspace_root=self.workspace_root,
            rules=[{
                "path": rule.path, "scope": rule.scope, "read_status": rule.read_status,
                "content": wrap_untrusted(rule.content, source=f"project:{rule.path}"),
            } for rule in self.rules],
        )


def selected_instruction_path(directory: Path) -> Path:
    """Presence wins over readability, including a broken standard symlink."""
    standard = directory / "AGENTS.md"
    legacy = directory / "agent.md"
    if os.path.lexists(standard) or not os.path.lexists(legacy):
        return standard
    return legacy


def load_project_rules(
    workspace_root: str,
    *,
    per_file_max_chars: int = 20_000,
    injection_scan_mode: str = "report",
) -> ProjectRulesSnapshot:
    """Read the bound root's selected instructions, preserving failure status."""
    root = Path(workspace_root).expanduser().absolute()
    try:
        resolved_root = root.resolve(strict=True)
        if resolved_root != root or not root.is_dir():
            raise ValueError("workspace binding changed")
    except (OSError, RuntimeError, ValueError) as exc:
        return ProjectRulesSnapshot(str(root), (
            ProjectRule(str(root), str(root), f"unavailable ({type(exc).__name__})"),
        ))
    candidate = selected_instruction_path(root)
    if not os.path.lexists(candidate):
        return ProjectRulesSnapshot(str(root))
    content = ""
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            status = "outside workspace; skipped"
        elif not resolved.is_file():
            raise ValueError("instruction candidate is not a file")
        elif not (budget := max(0, per_file_max_chars)):
            status = "budget exhausted; not read"
        else:
            with resolved.open(encoding="utf-8") as handle:
                content = handle.read(budget + 1)
            truncated = len(content) > budget
            content = content[:budget]
            status = (
                "truncated; read the original file for omitted instructions"
                if truncated else "complete"
            )
            scanned, _findings = scan_for_injection(
                content, f"project:{candidate}", mode=injection_scan_mode,
            )
            if scanned != content:
                status += "; injection scan applied"
            content = scanned
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        status = f"unreadable ({type(exc).__name__})"
        content = ""
    return ProjectRulesSnapshot(str(root), (
        ProjectRule(str(candidate), str(root), status, content),
    ))


@dataclass(frozen=True)
class ProjectInitRequest:
    workspace_root: str
    selected_path: str
    user_notes: str = field(repr=False)
    template: str = field(repr=False)

    @classmethod
    def capture(cls, workspace_root: Path, user_notes: str) -> ProjectInitRequest:
        return cls(
            str(workspace_root), str(selected_instruction_path(workspace_root)), user_notes,
            (_TEMPLATES / "project_init.md.j2").read_text(encoding="utf-8"),
        )

    def render(self, authorized_tools: Set[str]) -> str:
        return _make_env().from_string(self.template).render(
            workspace_root=self.workspace_root,
            selected_path=self.selected_path,
            user_notes=self.user_notes,
            can_save=SOURCE_TOOLS.issubset(authorized_tools),
        )
