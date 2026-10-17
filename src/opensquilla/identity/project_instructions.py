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
    default_cwd: str | None = None,
    per_file_max_chars: int = 20_000,
    total_max_chars: int = 50_000,
    injection_scan_mode: str = "report",
) -> ProjectRulesSnapshot:
    """Read only root-to-CWD candidates, preserving failures and scope."""
    root = Path(workspace_root).expanduser().absolute()
    rules: list[ProjectRule] = []
    try:
        resolved_root = root.resolve(strict=True)
        if resolved_root != root or not root.is_dir():
            raise ValueError("workspace binding changed")
        cwd = Path(default_cwd).expanduser().absolute() if default_cwd else root
        cwd = cwd.resolve(strict=True)
        relative = cwd.relative_to(root)
        if not cwd.is_dir():
            raise ValueError("working directory is not a directory")
    except (OSError, RuntimeError, ValueError) as exc:
        return ProjectRulesSnapshot(str(root), (
            ProjectRule(str(root), str(root), f"unavailable ({type(exc).__name__})"),
        ))
    directories = [root]
    for part in relative.parts:
        directories.append(directories[-1] / part)
    seen: set[tuple[object, ...]] = set()
    remaining = max(0, total_max_chars)
    for directory in directories:
        candidate = selected_instruction_path(directory)
        if not os.path.lexists(candidate):
            continue
        try:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root):
                rules.append(ProjectRule(
                    str(candidate), str(directory), "outside workspace; skipped",
                ))
                continue
            stat = resolved.stat()
            identity = (stat.st_dev, stat.st_ino) if stat.st_ino else (str(resolved),)
            if identity in seen:
                continue
            seen.add(identity)
            if not resolved.is_file():
                raise ValueError("instruction candidate is not a file")
            budget = min(max(0, per_file_max_chars), remaining)
            if not budget:
                rules.append(ProjectRule(
                    str(candidate), str(directory), "budget exhausted; not read",
                ))
                continue
            with resolved.open(encoding="utf-8") as handle:
                content = handle.read(budget + 1)
            truncated = len(content) > budget
            content = content[:budget]
            remaining -= len(content)
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
            rules.append(ProjectRule(str(candidate), str(directory), status, content))
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            rules.append(ProjectRule(
                str(candidate), str(directory), f"unreadable ({type(exc).__name__})",
            ))
    return ProjectRulesSnapshot(str(root), tuple(rules))


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
