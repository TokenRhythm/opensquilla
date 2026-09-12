"""State-aware guard for commands that would erase source diffs."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from opensquilla.tools.types import ToolContext, current_tool_context
from opensquilla.tools.write_tracking import (
    classify_workspace_path,
    snapshot_workspace_mutations,
)

SourceDiffPreservationMode = Literal["off", "log", "block"]

_SHELL_SEGMENT_BOUNDARY_TOKENS = frozenset({"&&", ";", "||", "|", "&"})
_GIT_OPTIONS_WITH_VALUE = frozenset(
    {
        "-b",
        "-B",
        "-c",
        "-C",
        "--conflict",
        "--orphan",
        "--pathspec-from-file",
        "--source",
        "--track",
        "-s",
    }
)


@dataclass(frozen=True)
class _ParsedDestructiveGitCommand:
    operation: str
    targets: tuple[str, ...] = ()
    whole_worktree: bool = False
    untracked_only: bool = False


@dataclass(frozen=True)
class SourceDiffPreservationDecision:
    """Decision for a potentially destructive source-diff command."""

    should_block: bool
    payload: dict[str, Any]


def source_diff_preservation_decision(
    *,
    command: str,
    workdir: str | Path | None,
    ctx: ToolContext | None = None,
) -> SourceDiffPreservationDecision | None:
    """Return a block/log decision for source-diff destructive commands.

    The guard is intentionally high-confidence. Complex shell scripts are left
    alone in v1 instead of being partially parsed incorrectly.
    """

    active = ctx if ctx is not None else current_tool_context.get()
    if active is None:
        return None
    mode = _normalize_mode(getattr(active, "source_diff_preservation_mode", "log"))
    if mode == "off":
        return None

    workspace = _workspace_root(active, workdir)
    if workspace is None:
        return None
    parsed = _parse_destructive_git_command(command)
    if parsed is None:
        return None

    protected = _protected_source_paths(active, workspace)
    if not protected:
        return None

    protected_source_paths = sorted(protected)
    untracked_source_paths = _protected_untracked_source_paths(active, workspace)
    if parsed.untracked_only:
        affected = _matching_targets(parsed, sorted(untracked_source_paths), workspace)
    else:
        affected = _matching_targets(parsed, protected_source_paths, workspace)
    if not affected:
        return None

    should_block = mode == "block"
    payload = {
        "status": "blocked" if should_block else "observed",
        "reason": "source_diff_revert_blocked"
        if should_block
        else "source_diff_revert_observed",
        "matched_operation": parsed.operation,
        "protected_source_paths": affected,
        "target_paths": list(parsed.targets),
        "retry_allowed": True,
        "recommended_next_action": (
            "Inspect git_diff and keep or edit the source patch instead of restoring it."
        ),
    }
    try:
        from opensquilla.tools.source_diff_candidates import (
            mark_source_diff_candidates_lost,
        )

        mark_source_diff_candidates_lost(
            ctx=active,
            paths=affected,
            reason=str(payload["reason"]),
            command=command,
        )
    except Exception:
        pass
    _emit_preservation_event(
        active,
        payload,
        event_name="source_diff_revert_blocked"
        if should_block
        else "source_diff_revert_observed",
    )
    return SourceDiffPreservationDecision(should_block=should_block, payload=payload)


def source_diff_preservation_block_json(
    *,
    command: str,
    workdir: str | Path | None,
    ctx: ToolContext | None = None,
) -> str | None:
    decision = source_diff_preservation_decision(command=command, workdir=workdir, ctx=ctx)
    if decision is None or not decision.should_block:
        return None
    return json.dumps(decision.payload, ensure_ascii=False)


def _normalize_mode(raw: object) -> SourceDiffPreservationMode:
    value = str(raw or "log").strip().lower()
    if value in {"off", "log", "block"}:
        return value  # type: ignore[return-value]
    return "log"


def _workspace_root(active: ToolContext, workdir: str | Path | None) -> Path | None:
    raw = active.workspace_dir or workdir
    if raw is None:
        return None
    return Path(raw).expanduser().resolve(strict=False)


def _parse_destructive_git_command(
    command: str,
) -> _ParsedDestructiveGitCommand | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None
    for segment in _shell_sequence_segments(tokens):
        parsed = _parse_git_segment(segment)
        if parsed is not None:
            return parsed
    return None


def _shell_sequence_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_SEGMENT_BOUNDARY_TOKENS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _parse_git_segment(tokens: list[str]) -> _ParsedDestructiveGitCommand | None:
    tokens = _strip_shell_redirections(tokens)
    if len(tokens) < 2 or tokens[0] != "git":
        return None

    verb = tokens[1]
    args = tokens[2:]
    if verb == "restore":
        targets = _pathspecs_after_options(args, options_with_value={"--source", "-s"})
        return _ParsedDestructiveGitCommand(
            operation="git_restore",
            targets=tuple(targets),
            whole_worktree=_has_whole_worktree_target(targets),
        )
    if verb == "checkout":
        return _parse_git_checkout(args)
    if verb == "reset" and _has_reset_hard(args):
        return _ParsedDestructiveGitCommand(
            operation="git_reset_hard",
            whole_worktree=True,
        )
    if verb == "clean" and _has_git_clean_force_delete(args):
        targets = _pathspecs_after_options(args)
        return _ParsedDestructiveGitCommand(
            operation="git_clean",
            targets=tuple(targets),
            whole_worktree=not targets or _has_whole_worktree_target(targets),
            untracked_only=True,
        )
    return None


def _strip_shell_redirections(tokens: list[str]) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {">", ">>", "<", "2>", "2>>", "1>", "1>>", "&>", "&>>"}:
            index += 2
            continue
        if _is_shell_redirection_token(token):
            index += 1
            continue
        result.append(token)
        index += 1
    return result


def _is_shell_redirection_token(token: str) -> bool:
    return ">" in token or "<" in token


def _parse_git_checkout(args: list[str]) -> _ParsedDestructiveGitCommand | None:
    force = any(arg in {"-f", "--force"} for arg in args)
    if "--" in args:
        marker = args.index("--")
        targets = args[marker + 1 :]
        return _ParsedDestructiveGitCommand(
            operation="git_checkout",
            targets=tuple(targets),
            whole_worktree=_has_whole_worktree_target(targets),
        )
    remaining = _pathspecs_after_options(args)
    if force and not _looks_like_path_checkout(remaining):
        return _ParsedDestructiveGitCommand(
            operation="git_checkout_force",
            whole_worktree=True,
        )
    if not remaining:
        return None
    if len(remaining) == 1:
        targets = remaining
    else:
        targets = remaining[1:]
    if not targets:
        return None
    return _ParsedDestructiveGitCommand(
        operation="git_checkout",
        targets=tuple(targets),
        whole_worktree=_has_whole_worktree_target(targets),
    )


def _pathspecs_after_options(
    args: list[str],
    *,
    options_with_value: set[str] | None = None,
) -> list[str]:
    options_requiring_value = set(_GIT_OPTIONS_WITH_VALUE)
    if options_with_value:
        options_requiring_value |= options_with_value
    result: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            result.extend(args[index + 1 :])
            break
        if arg in options_requiring_value:
            index += 2
            continue
        if arg.startswith("--") and "=" in arg:
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        result.append(arg)
        index += 1
    return result


def _has_whole_worktree_target(targets: list[str] | tuple[str, ...]) -> bool:
    return any(target in {"", ".", "./", ":/"} for target in targets)


def _looks_like_path_checkout(args: list[str]) -> bool:
    if not args:
        return False
    if _has_whole_worktree_target(args):
        return True
    return any("/" in arg or arg.startswith((".", "~")) for arg in args)


def _has_reset_hard(args: list[str]) -> bool:
    return any(arg == "--hard" or arg.startswith("--hard=") for arg in args)


def _has_git_clean_force_delete(args: list[str]) -> bool:
    force = False
    delete_dirs = False
    for arg in args:
        if arg in {"-f", "--force"}:
            force = True
        elif arg == "-d":
            delete_dirs = True
        elif arg.startswith("-") and not arg.startswith("--"):
            force = force or "f" in arg
            delete_dirs = delete_dirs or "d" in arg
    return force and delete_dirs


def _protected_source_paths(active: ToolContext, workspace: Path) -> set[str]:
    paths = _changed_source_receipt_paths(active)
    paths.update(_current_source_diff_paths(workspace))
    return paths


def _protected_untracked_source_paths(active: ToolContext, workspace: Path) -> set[str]:
    paths = {
        path
        for path, status in _current_source_status_paths(workspace).items()
        if status == "??"
    }
    for receipt in getattr(active, "workspace_mutation_receipts", []) or []:
        if receipt.get("changed") is not True:
            continue
        if str(receipt.get("classification") or "") != "source":
            continue
        relative_path = _receipt_relative_path(receipt)
        before = receipt.get("before")
        if relative_path and isinstance(before, dict) and before.get("exists") is False:
            paths.add(relative_path)
    return paths


def _changed_source_receipt_paths(active: ToolContext) -> set[str]:
    paths: set[str] = set()
    for receipt in getattr(active, "workspace_mutation_receipts", []) or []:
        if receipt.get("changed") is not True:
            continue
        if str(receipt.get("classification") or "") != "source":
            continue
        relative_path = _receipt_relative_path(receipt)
        if relative_path:
            paths.add(relative_path)
    return paths


def _receipt_relative_path(receipt: dict[str, Any]) -> str | None:
    raw = receipt.get("relative_path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return _normalize_relative_path(raw)


def _current_source_diff_paths(workspace: Path) -> set[str]:
    return set(_current_source_status_paths(workspace))


def _current_source_status_paths(workspace: Path) -> dict[str, str]:
    paths: dict[str, str] = {}
    for relative_path, status in snapshot_workspace_mutations(workspace).items():
        normalized = _normalize_relative_path(relative_path)
        if classify_workspace_path(normalized) == "source":
            paths[normalized] = status
    return paths


def _matching_targets(
    parsed: _ParsedDestructiveGitCommand,
    protected_paths: list[str],
    workspace: Path,
) -> list[str]:
    if not protected_paths:
        return []
    if parsed.whole_worktree:
        return protected_paths
    normalized_targets = [
        target
        for target in (
            _normalize_pathspec(target, workspace=workspace) for target in parsed.targets
        )
        if target
    ]
    if not normalized_targets:
        return []
    return [
        protected
        for protected in protected_paths
        if any(_target_matches_protected(target, protected) for target in normalized_targets)
    ]


def _normalize_pathspec(target: str, *, workspace: Path) -> str | None:
    target = target.strip()
    if not target:
        return None
    if target in {".", "./", ":/"}:
        return "."
    path = Path(target).expanduser()
    if path.is_absolute():
        try:
            return path.resolve(strict=False).relative_to(workspace).as_posix()
        except ValueError:
            return None
    return _normalize_relative_path(target)


def _normalize_relative_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _target_matches_protected(target: str, protected: str) -> bool:
    if target == ".":
        return True
    normalized_target = _normalize_relative_path(target)
    normalized_protected = _normalize_relative_path(protected)
    return normalized_protected == normalized_target or normalized_protected.startswith(
        normalized_target.rstrip("/") + "/"
    )


def _emit_preservation_event(
    active: ToolContext,
    payload: dict[str, Any],
    *,
    event_name: str,
) -> None:
    callback = getattr(active, "on_runtime_event", None)
    if callback is None:
        return
    try:
        callback(
            {
                "feature": "source_diff_preservation",
                "name": event_name,
                "reason": payload.get("reason"),
                "matched_operation": payload.get("matched_operation"),
                "protected_source_paths": payload.get("protected_source_paths", []),
                "protected_source_path_count": len(
                    payload.get("protected_source_paths", []) or []
                ),
                "target_paths": payload.get("target_paths", []),
                "status": payload.get("status"),
                "session_key": getattr(active, "session_key", None),
                "agent_id": getattr(active, "agent_id", None),
            }
        )
    except Exception:
        return
