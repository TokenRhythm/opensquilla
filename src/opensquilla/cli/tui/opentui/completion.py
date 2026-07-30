"""Completion catalog and file-reference helpers for the OpenTUI composer."""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from opensquilla.engine.commands import CommandPresentation, Surface
from opensquilla.tools.builtin.filesystem import _is_sensitive_access_path

from .messages import (
    CompletionArgumentChoice,
    CompletionCandidate,
    CompletionContext,
)

_SEGMENT_SEPARATORS = frozenset("/\\._- ")
_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__"})


class SkillCompletionLoader(Protocol):
    def get_user_invocable(self) -> Sequence[Any]: ...


def fuzzy_rank(query: str, candidates: Sequence[str]) -> list[tuple[int, float]]:
    """Return matching candidate indexes ranked by deterministic fuzzy score.

    Matching is case-insensitive and requires the query to appear as an ordered
    subsequence of the candidate. Scores then prefer, in order: exact prefix,
    slash-command segment prefix, or path-segment prefix matches, longer
    contiguous matched runs, characters matched at the beginning of a path
    segment, earlier first matches, and shorter matched path
    segments/candidates. The scoring mirrors ``fuzzyScore`` in
    ``package/src/composer.mjs`` so the host's local ranking and this one agree.
    Ties keep the input order. Empty queries return every candidate in the
    original order with a neutral score.
    """

    normalized_query = query.casefold()
    if not normalized_query:
        return [(index, 0.0) for index, _candidate in enumerate(candidates)]

    scored: list[tuple[int, float]] = []
    for index, candidate in enumerate(candidates):
        score = _score_candidate(normalized_query, candidate)
        if score is not None:
            scored.append((index, score))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


def fuzzy_filter[T](
    query: str,
    items: Sequence[T],
    *,
    key: Callable[[T], str] = str,
) -> list[T]:
    """Filter and rank items with :func:`fuzzy_rank` using ``key`` for text."""

    if not query:
        return list(items)
    candidate_text = [key(item) for item in items]
    return [items[index] for index, _score in fuzzy_rank(query, candidate_text)]


def enumerate_workspace_files(
    root: Path,
    *,
    query: str = "",
    max_results: int = 50,
    max_walk: int = 20_000,
) -> list[str]:
    """Return workspace-relative POSIX file paths suitable for ``@`` completion."""

    resolved_root = root.expanduser().resolve(strict=False)
    candidates = _git_files(resolved_root)
    if candidates is None:
        candidates = _walk_files(resolved_root, max_walk=max_walk)
    else:
        candidates = _filter_sensitive_relative_paths(resolved_root, candidates)

    return fuzzy_filter(query, candidates)[:max_results]


def build_completion_catalog(
    *,
    surface: Surface | str,
    skill_loader: SkillCompletionLoader | None = None,
    workspace_dir: Path | None = None,
) -> list[CompletionCandidate]:
    """Build command, skill, and setting rows for slash completion."""

    return [
        *_command_candidates(surface),
        *_skill_candidates(skill_loader, workspace_dir=workspace_dir),
    ]


def build_completion_context(
    surface: Surface | str,
    *,
    skill_loader: SkillCompletionLoader | None = None,
    workspace_dir: Path | None = None,
    file_query: str = "",
    max_files: int = 50,
    max_walk: int = 20_000,
) -> CompletionContext:
    """Build typed completion metadata for the OpenTUI host."""

    files: tuple[str, ...] = ()
    if workspace_dir is not None:
        try:
            files = tuple(
                enumerate_workspace_files(
                    workspace_dir,
                    query=file_query,
                    max_results=max_files,
                    max_walk=max_walk,
                )
            )
        except Exception:
            files = ()
    return CompletionContext(
        catalog=tuple(
            build_completion_catalog(
                surface=surface,
                skill_loader=skill_loader,
                workspace_dir=workspace_dir,
            )
        ),
        files=files,
        filters_sensitive_paths=True,
    )


def _score_candidate(query: str, candidate: str) -> float | None:
    # Mirrors fuzzyScore in package/src/composer.mjs: the host ranks its local
    # files snapshot with that scorer before this side's async response
    # replaces the menu, so any drift shows up as a visible reorder flicker.
    text = candidate.casefold()
    positions = _subsequence_positions(query, text)
    if positions is None:
        return None

    score = float(len(query) * 100)
    if text.startswith(query):
        score += 80

    segments = _path_segments(text)
    if text.startswith("/") and segments and segments[0].startswith(query):
        score += 90
    prefix_segment = next((segment for segment in segments if segment.startswith(query)), None)
    if prefix_segment is not None:
        score += 60
        score += max(0.0, 24.0 - (len(prefix_segment) * 2.0))

    run_length = 1
    longest_run = 1
    for left, right in zip(positions, positions[1:]):
        if right == left + 1:
            run_length += 1
            longest_run = max(longest_run, run_length)
        else:
            run_length = 1
    score += longest_run * longest_run * 8

    for position in positions:
        if _is_segment_start(text, position):
            score += 18

    first = positions[0]
    score += max(0.0, 30.0 - (first * 0.75))
    score += max(0.0, 18.0 - len(candidate) * 0.35)
    return score


def _subsequence_positions(query: str, text: str) -> list[int] | None:
    positions: list[int] = []
    start = 0
    for char in query:
        index = text.find(char, start)
        if index < 0:
            return None
        positions.append(index)
        start = index + 1
    return positions


def _path_segments(text: str) -> list[str]:
    normalized = text.replace("\\", "/")
    segments: list[str] = []
    for slash_part in normalized.split("/"):
        current: list[str] = []
        for char in slash_part:
            if char in "._- ":
                if current:
                    segments.append("".join(current))
                    current = []
            else:
                current.append(char)
        if current:
            segments.append("".join(current))
    return segments


def _is_segment_start(text: str, position: int) -> bool:
    return position == 0 or text[position - 1] in _SEGMENT_SEPARATORS


def _git_files(root: Path) -> list[str] | None:
    if not (root / ".git").exists() or shutil.which("git") is None:
        return None

    try:
        # -z: NUL-separated verbatim paths, so core.quotePath never C-quotes a
        # non-ASCII name into an octal-escape string that matches nothing on
        # disk. Keep the platform's normal filesystem decoding first, then fall
        # back to surrogateescape because Windows' surrogatepass handler can
        # raise for malformed UTF-8 bytes. Completion must remain available for
        # every Git path without changing valid Windows path decoding.
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return [
        Path(_decode_git_path(entry)).as_posix() for entry in result.stdout.split(b"\0") if entry
    ]


def _decode_git_path(entry: bytes) -> str:
    try:
        return os.fsdecode(entry)
    except UnicodeDecodeError:
        return entry.decode(sys.getfilesystemencoding(), errors="surrogateescape")


def _walk_files(root: Path, *, max_walk: int) -> list[str]:
    ignore_rules = _load_gitignore_patterns(root)
    results: list[str] = []
    visited = 0

    for dirpath, dirnames, filenames in os.walk(root):
        current_dir = Path(dirpath)
        dirnames[:] = [
            dirname
            for dirname in sorted(dirnames)
            if not _skip_dir(root, current_dir / dirname, ignore_rules)
        ]

        for filename in sorted(filenames):
            visited += 1
            if visited > max_walk:
                return sorted(results)

            path = current_dir / filename
            rel = path.relative_to(root).as_posix()
            if _is_ignored(rel, ignore_rules):
                continue
            if _is_sensitive_access_path(path.resolve(strict=False)):
                continue
            results.append(rel)

    return sorted(results)


def _filter_sensitive_relative_paths(root: Path, rel_paths: list[str]) -> list[str]:
    results: list[str] = []
    for rel in rel_paths:
        path = root / rel
        if _is_sensitive_access_path(path.resolve(strict=False)):
            continue
        results.append(Path(rel).as_posix())
    return results


def _skip_dir(root: Path, path: Path, ignore_rules: list[tuple[str, bool]]) -> bool:
    name = path.name
    if name in _SKIP_DIRS or name.startswith("."):
        return True
    rel = path.relative_to(root).as_posix()
    return _is_ignored(f"{rel}/", ignore_rules) or _is_ignored(rel, ignore_rules)


def _load_gitignore_patterns(root: Path) -> list[tuple[str, bool]]:
    """Parse the root ``.gitignore`` into ordered ``(pattern, negated)`` rules."""
    gitignore = root / ".gitignore"
    try:
        lines = gitignore.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []

    rules: list[tuple[str, bool]] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
            if not line:
                continue
        rules.append((line.lstrip("/"), negated))
    return rules


def _is_ignored(rel_posix: str, rules: list[tuple[str, bool]]) -> bool:
    # Git semantics: the LAST matching rule wins, so a later "!keep.log" can
    # re-include a file excluded by an earlier "*.log" (and vice versa).
    rel = rel_posix.strip("/")
    parts = rel.split("/") if rel else []
    ignored = False
    for pattern, negated in rules:
        if _pattern_matches(rel, parts, pattern):
            ignored = not negated
    return ignored


def _pattern_matches(rel: str, parts: list[str], pattern: str) -> bool:
    normalized = pattern.strip("/")
    if not normalized:
        return False
    if pattern.endswith("/") and (rel == normalized or rel.startswith(normalized + "/")):
        return True
    if "/" in normalized:
        return fnmatch.fnmatch(rel, normalized) or rel.startswith(normalized + "/")
    if fnmatch.fnmatch(Path(rel).name, normalized):
        return True
    return any(fnmatch.fnmatch(part, normalized) for part in parts)


def _command_candidates(surface: Surface | str) -> list[CompletionCandidate]:
    from opensquilla.engine.commands import DEFAULT_REGISTRY

    commands = list(DEFAULT_REGISTRY.for_surface(surface))
    commands.sort(key=lambda command: getattr(command, "order", 10_000))
    return [
        CompletionCandidate(
            label=command.name,
            description=command.description_for(surface),
            insert_text=f"{command.name} ",
            category=str(getattr(command, "category", "command")),
            usage=command.usage_for(surface),
            aliases=tuple(command.aliases),
            argument_choices=tuple(
                CompletionArgumentChoice(
                    value=choice.value,
                    description=choice.description,
                )
                for choice in command.argument_choices_for(surface)
            ),
            visible_by_default=bool(getattr(command, "visible_by_default", True)),
            deprecated=bool(getattr(command, "deprecated", False)),
            submit_behavior=(
                "complete"
                if "<" in command.usage_for(surface)
                and command.presentation is not CommandPresentation.PICKER
                else "submit"
            ),
            busy_policy=str(getattr(command, "busy_policy", "immediate")),
            presentation=str(getattr(command, "presentation", "notice")),
        )
        for command in commands
    ]


def _skill_candidates(
    skill_loader: SkillCompletionLoader | None,
    *,
    workspace_dir: Path | None,
) -> list[CompletionCandidate]:
    try:
        loader = (
            skill_loader
            if skill_loader is not None
            else _build_skill_loader(workspace_dir=workspace_dir)
        )
        skills = loader.get_user_invocable()
    except Exception:
        return []

    candidates: list[CompletionCandidate] = []
    for skill in sorted(skills, key=lambda item: getattr(item, "name", "")):
        if getattr(skill, "disable_model_invocation", False):
            continue
        name = str(getattr(skill, "name", "")).strip()
        if not name:
            continue
        candidates.append(
            CompletionCandidate(
                label=f"/skill:{name.lstrip('/')}",
                description=str(getattr(skill, "description", "")),
                insert_text=f"use the {name.lstrip('/')} skill: ",
                category="skill",
                submit_behavior="complete",
            )
        )
    return candidates


def _build_skill_loader(*, workspace_dir: Path | None = None) -> SkillCompletionLoader:
    import os as _os

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.skills.paths import resolve_skill_layer_dirs

    config = GatewayConfig.load(_os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"))
    workspace_root = (
        workspace_dir
        if workspace_dir is not None
        else Path(config.workspace_dir)
        if config.workspace_dir
        else None
    )
    workspace_override = Path(config.skills.workspace_dir) if config.skills.workspace_dir else None
    layer_dirs = resolve_skill_layer_dirs(
        allow_bundled=config.skills.allow_bundled,
        workspace_root=workspace_root,
        workspace_override=workspace_override,
        managed_override=config.skills.managed_dir,
        extra_dirs=[Path(d) for d in config.skills.extra_dirs],
    )
    return SkillLoader(
        bundled_dir=layer_dirs.bundled_dir,
        workspace_dir=layer_dirs.workspace_dir,
        managed_dir=layer_dirs.managed_dir,
        personal_agents_dir=layer_dirs.personal_agents_dir,
        project_agents_dir=layer_dirs.project_agents_dir,
        extra_dirs=layer_dirs.extra_dirs,
    )


__all__ = [
    "CompletionCandidate",
    "build_completion_catalog",
    "build_completion_context",
    "enumerate_workspace_files",
    "fuzzy_filter",
    "fuzzy_rank",
]
