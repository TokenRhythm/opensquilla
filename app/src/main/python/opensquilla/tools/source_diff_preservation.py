"""State-aware guard for commands that would erase source diffs."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from opensquilla.git_runtime import run_git
from opensquilla.tools.patch_classification import is_instrumentation_only_patch
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


# `git stash` subcommands that move or drop worktree changes. Bare `git stash`
# (implicit push) and option-only forms like `git stash -u` count as push.
_DESTRUCTIVE_GIT_STASH_SUBCOMMANDS = frozenset({"push", "save", "drop", "clear"})


def endgame_git_freeze_decision(
    *,
    command: str,
    ctx: ToolContext | None = None,
) -> dict[str, Any] | None:
    """Return a block payload for destructive git commands during the freeze.

    Armed by the engine (ToolContext.endgame_git_freeze_active) once remaining
    wall-clock time drops below OPENSQUILLA_ENDGAME_GIT_FREEZE_MARGIN_SECONDS.
    Unlike source_diff_preservation_decision there is no protected-path
    intersection: every parsed workspace-reverting operation — including
    branch switches and stashes — is blocked outright so the current
    workspace diff survives runner-side collection.
    """

    active = ctx if ctx is not None else current_tool_context.get()
    if active is None:
        return None
    if getattr(active, "endgame_git_freeze_active", False) is not True:
        return None
    parsed = _parse_endgame_frozen_git_command(command)
    if parsed is None:
        return None
    if (
        getattr(active, "endgame_git_freeze_instrumentation_exempt", False)
        and not parsed.untracked_only
        and not _freeze_exemption_scope_beyond_head(command)
    ):
        exempt_diff = _freeze_exemption_diff(active, parsed)
        if exempt_diff is not None and is_instrumentation_only_patch(exempt_diff):
            _emit_freeze_exemption_event(active, parsed, command=command)
            return None
    payload: dict[str, Any] = {
        "status": "blocked",
        "reason": "endgame_git_freeze",
        "matched_operation": parsed.operation,
        "target_paths": list(parsed.targets),
        "retry_allowed": True,
        "recommended_next_action": (
            "The run is in its final wrap-up window and workspace-reverting git "
            "commands are frozen so the pending changes stay intact. Keep "
            "or edit the existing changes in place instead of restoring, "
            "resetting, cleaning, stashing, or switching branches."
        ),
    }
    _emit_freeze_event(active, payload, command=command)
    return payload


def endgame_git_freeze_block_json(
    *,
    command: str,
    ctx: ToolContext | None = None,
) -> str | None:
    payload = endgame_git_freeze_decision(command=command, ctx=ctx)
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False)


def _parse_endgame_frozen_git_command(
    command: str,
    depth: int = 0,
) -> _ParsedDestructiveGitCommand | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None
    for raw_segment in _shell_sequence_segments(tokens):
        segment = _strip_git_global_options(_strip_shell_redirections(raw_segment))
        parsed = _parse_git_segment(segment)
        if parsed is None:
            parsed = _parse_git_stash_segment(segment)
        if parsed is None:
            parsed = _parse_git_switch_segment(segment)
        if parsed is not None:
            return parsed
        if depth < 2:
            wrapped = _shell_wrapper_command(segment)
            if wrapped is not None:
                parsed = _parse_endgame_frozen_git_command(wrapped, depth + 1)
                if parsed is not None:
                    return parsed
    return None


# git global options the freeze parser skips before reading the verb. Applied
# in the freeze path only, so shared source_diff_preservation parsing is
# untouched.
_FREEZE_GIT_GLOBAL_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree"})


def _strip_git_global_options(tokens: list[str]) -> list[str]:
    if not tokens or tokens[0] != "git":
        return tokens
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in _FREEZE_GIT_GLOBAL_VALUE_OPTIONS:
            index += 2
            continue
        if token == "--no-pager" or token.startswith(("--git-dir=", "--work-tree=")):
            index += 1
            continue
        break
    if index == 1:
        return tokens
    return ["git", *tokens[index:]]


_SHELL_WRAPPER_NAMES = frozenset({"sh", "bash", "zsh", "dash", "ksh"})


def _shell_wrapper_command(tokens: list[str]) -> str | None:
    # Freeze-only extension: `sh -c "git checkout -- ."` reverts just as
    # effectively as the bare command.
    if not tokens:
        return None
    name = tokens[0].replace("\\", "/").rsplit("/", 1)[-1]
    if name not in _SHELL_WRAPPER_NAMES:
        return None
    take_next = False
    for token in tokens[1:]:
        if take_next:
            return token
        if token.startswith("--"):
            continue
        if token.startswith("-") and len(token) > 1 and token.endswith("c"):
            take_next = True
            continue
        if token.startswith("-"):
            continue
        return None
    return None


def _parse_git_stash_segment(tokens: list[str]) -> _ParsedDestructiveGitCommand | None:
    # Freeze-only extension: the shared _parse_git_segment intentionally stays
    # unchanged so default source_diff_preservation behavior is untouched.
    tokens = _strip_shell_redirections(tokens)
    if len(tokens) < 2 or tokens[0] != "git" or tokens[1] != "stash":
        return None
    subcommand = None
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if token in ("-m", "--message"):
            # The option value is a message, not a subcommand:
            # `git stash -m wip` is still an implicit push.
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        subcommand = token
        break
    if subcommand is None or subcommand in _DESTRUCTIVE_GIT_STASH_SUBCOMMANDS:
        return _ParsedDestructiveGitCommand(operation="git_stash", whole_worktree=True)
    return None


def _parse_git_switch_segment(tokens: list[str]) -> _ParsedDestructiveGitCommand | None:
    # Freeze-only extension: plain `git switch` refuses to clobber local
    # changes on its own, so only the change-discarding forms are frozen.
    tokens = _strip_shell_redirections(tokens)
    if len(tokens) < 2 or tokens[0] != "git" or tokens[1] != "switch":
        return None
    if any(arg in {"-f", "--force", "--discard-changes"} for arg in tokens[2:]):
        return _ParsedDestructiveGitCommand(
            operation="git_switch_force", whole_worktree=True
        )
    return None


def _emit_freeze_event(
    active: ToolContext,
    payload: dict[str, Any],
    *,
    command: str,
) -> None:
    callback = getattr(active, "on_runtime_event", None)
    if callback is None:
        return
    try:
        callback(
            {
                "feature": "endgame_git_freeze",
                "name": "endgame_git_freeze.blocked",
                "reason": payload.get("reason"),
                "matched_operation": payload.get("matched_operation"),
                "target_paths": payload.get("target_paths", []),
                "status": payload.get("status"),
                "command": command,
                "session_key": getattr(active, "session_key", None),
                "agent_id": getattr(active, "agent_id", None),
            }
        )
    except Exception:
        return


def _freeze_exemption_scope_beyond_head(command: str, depth: int = 0) -> bool:
    """True when a frozen command's revert scope exceeds worktree-vs-HEAD.

    The instrumentation exemption models the command's damage as
    ``git diff HEAD``; a command that restores content from another ref
    (``checkout <ref> --``, ``restore --source=<ref>``) or moves HEAD itself
    (``reset --hard HEAD~n``, force switch) destroys committed state that
    diff cannot see, so the exemption must never apply. Every shell segment
    and wrapper level is checked, because the freeze blocks the whole
    compound command on its first destructive segment while a later segment
    may be the ref-moving one. Ambiguous forms count as beyond-HEAD: the
    result is the ordinary freeze block, never a lost exemption of committed
    work.
    """

    try:
        tokens = shlex.split(command)
    except ValueError:
        return True
    for raw_segment in _shell_sequence_segments(tokens):
        segment = _strip_git_global_options(_strip_shell_redirections(raw_segment))
        if _git_segment_scope_beyond_head(segment):
            return True
        if depth < 2:
            wrapped = _shell_wrapper_command(segment)
            if wrapped is not None and _freeze_exemption_scope_beyond_head(
                wrapped, depth + 1
            ):
                return True
    return False


def _git_segment_scope_beyond_head(segment: list[str]) -> bool:
    if len(segment) < 2 or segment[0] != "git":
        return False
    verb = segment[1]
    args = segment[2:]
    if verb == "checkout":
        return _checkout_scope_beyond_head(args)
    if verb == "restore":
        return _restore_scope_beyond_head(args)
    if verb == "switch":
        # The freeze only parses the change-discarding forms; all of them
        # exist to move HEAD to another branch.
        return any(arg in {"-f", "--force", "--discard-changes"} for arg in args)
    if verb == "reset":
        if not _has_reset_hard(args):
            return False
        return any(token != "HEAD" for token in _pathspecs_after_options(args))
    return False


def _checkout_scope_beyond_head(args: list[str]) -> bool:
    if "-" in args:
        # `git checkout [-f] -` switches to the previous branch.
        return True
    if "--" in args:
        marker = args.index("--")
        before = _pathspecs_after_options(args[:marker])
        return any(token != "HEAD" for token in before)
    remaining = _pathspecs_after_options(args)
    if not remaining:
        return False
    if remaining[0] == "HEAD":
        return False
    # Without `--` the first bare token can be a branch, a tag, a -B start
    # point, or a path restored from the index; only the path form stays
    # within worktree-vs-HEAD scope and it cannot be told apart
    # syntactically.
    return True


def _restore_scope_beyond_head(args: list[str]) -> bool:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        if arg in {"--source", "-s"}:
            source = args[index + 1] if index + 1 < len(args) else None
            return source != "HEAD"
        if arg.startswith("--source="):
            return arg.split("=", 1)[1] != "HEAD"
        if arg.startswith("-s") and arg not in {"-s"} and not arg.startswith("--"):
            return arg[2:] != "HEAD"
        index += 1
    return False


def _freeze_exemption_diff(
    active: ToolContext,
    parsed: _ParsedDestructiveGitCommand,
) -> str | None:
    """Diff of what the frozen command would revert, or None to keep blocking.

    Untracked-only operations never reach here (nothing tracked to classify);
    any probe failure also returns None so the freeze stays in force.
    """

    workspace = _workspace_root(active, None)
    if workspace is None:
        return None
    argv = ["git", "diff", "HEAD"]
    if parsed.targets and not parsed.whole_worktree:
        argv.extend(["--", *parsed.targets])
    result = run_git(argv[1:], cwd=workspace, timeout=5.0)
    if not result.ok:
        return None
    return result.stdout_text


def _emit_freeze_exemption_event(
    active: ToolContext,
    parsed: _ParsedDestructiveGitCommand,
    *,
    command: str,
) -> None:
    callback = getattr(active, "on_runtime_event", None)
    if callback is None:
        return
    try:
        callback(
            {
                "feature": "endgame_git_freeze",
                "name": "endgame_git_freeze.exempted",
                "reason": "instrumentation_only_diff",
                "matched_operation": parsed.operation,
                "target_paths": list(parsed.targets),
                "status": "exempted",
                "command": command,
                "session_key": getattr(active, "session_key", None),
                "agent_id": getattr(active, "agent_id", None),
            }
        )
    except Exception:
        return


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
