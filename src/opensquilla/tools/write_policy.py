"""Helpers for request-scoped workspace write deny rules."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from opensquilla.git_runtime import GitRunState, run_git
from opensquilla.tools.types import SafeToolError, ToolContext, current_tool_context

# Env levers in the workspace-write-deny family. Values are read at dispatch
# time (tools layer, no AgentConfig field); validate_workspace_write_deny_env
# gives deployments a strict bootstrap-time check so a typo fails the run at
# startup instead of silently disabling enforcement.
_WRITE_DENY_EFFECT_ENV = "OPENSQUILLA_WORKSPACE_WRITE_DENY_EFFECT"
_WRITE_DENY_EFFECT_MODES = ("off", "warn", "revert")
_WRITE_DENY_TRACKED_ONLY_ENV = "OPENSQUILLA_WORKSPACE_WRITE_DENY_TRACKED_ONLY"
_WRITE_DENY_SYMLINK_GUARD_ENV = "OPENSQUILLA_WORKSPACE_WRITE_DENY_SYMLINK_GUARD"
_WRITE_DENY_BOOL_ENVS = (
    _WRITE_DENY_TRACKED_ONLY_ENV,
    _WRITE_DENY_SYMLINK_GUARD_ENV,
    "OPENSQUILLA_WORKSPACE_WRITE_DENY_HOST_SHELL",
    "OPENSQUILLA_WORKSPACE_WRITE_DENY_COMMAND_TARGETS",
    "OPENSQUILLA_WORKSPACE_WRITE_DENY_INTERPRETER_TARGETS",
)
_WRITE_DENY_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})
_WRITE_DENY_FALSE_VALUES = frozenset({"", "0", "false", "no", "off", "disabled"})


def workspace_write_deny_effect_mode() -> str:
    """Post-execution effect enforcement mode: off (default), warn, or revert.

    Dispatch-time reads fail safe (unrecognized -> off); strict rejection of
    unrecognized values happens once at bootstrap via
    validate_workspace_write_deny_env.
    """

    raw = os.environ.get(_WRITE_DENY_EFFECT_ENV, "").strip().lower()
    if raw in _WRITE_DENY_EFFECT_MODES:
        return raw
    return "off"


def workspace_write_deny_tracked_only() -> bool:
    raw = os.environ.get(_WRITE_DENY_TRACKED_ONLY_ENV, "").strip().lower()
    return raw in _WRITE_DENY_TRUE_VALUES


def workspace_write_deny_symlink_guard() -> bool:
    raw = os.environ.get(_WRITE_DENY_SYMLINK_GUARD_ENV, "").strip().lower()
    return raw in _WRITE_DENY_TRUE_VALUES


def validate_workspace_write_deny_env() -> None:
    """Strictly validate the write-deny env lever family; raise on typos.

    Called from engine bootstrap so an unrecognized value stops the run at
    startup. The tools layer itself stays lenient (fail-safe to off) because
    it can run outside the engine.
    """

    raw = os.environ.get(_WRITE_DENY_EFFECT_ENV, "").strip().lower()
    if raw and raw not in _WRITE_DENY_EFFECT_MODES:
        raise ValueError(
            f"{_WRITE_DENY_EFFECT_ENV} must be one of "
            f"{', '.join(_WRITE_DENY_EFFECT_MODES)}; got {raw!r}"
        )
    for name in _WRITE_DENY_BOOL_ENVS:
        value = os.environ.get(name, "").strip().lower()
        if value not in _WRITE_DENY_TRUE_VALUES | _WRITE_DENY_FALSE_VALUES:
            raise ValueError(
                f"{name} must be a boolean flag "
                f"(one of {sorted(_WRITE_DENY_TRUE_VALUES | _WRITE_DENY_FALSE_VALUES)}); "
                f"got {value!r}"
            )


@dataclass(frozen=True)
class WorkspaceWriteDenyMatch:
    pattern: str
    path: str
    resolved_path: str


@dataclass(frozen=True)
class WorkspaceScratchArtifactMatch:
    path: str
    resolved_path: str
    scratch_dir: str


_ROOT_DIAGNOSTIC_ARTIFACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:debug|repro|reproduce|scratch|verify|inspect|investigate|trace|"
        r"analy[sz]e|analysis)(?:[_.-].*)?"
        r"\.(?:py|js|mjs|cjs|ts|rb|php|sh|txt|md|json|ya?ml|patch|diff|zsh)$",
        re.I,
    ),
    re.compile(
        r"^(?:check|fix|test)[_.-]"
        r"(?:bug|debug|failure|failing|issue|local|repro|scratch|temp|test|tmp|"
        r"verify|php|py|js|ts)(?:[_.-].*)?"
        r"\.(?:py|js|mjs|cjs|ts|rb|php|sh|txt|md|json|ya?ml|patch|diff|zsh)$",
        re.I,
    ),
)


def _workspace_write_deny_globs(ctx: ToolContext | None = None) -> tuple[str, ...]:
    active = ctx if ctx is not None else current_tool_context.get()
    if active is None:
        return ()
    patterns = getattr(active, "workspace_write_deny_globs", None) or []
    return tuple(str(pattern).strip() for pattern in patterns if str(pattern).strip())


def _workspace_root(ctx: ToolContext | None) -> Path | None:
    active = ctx if ctx is not None else current_tool_context.get()
    if active is None or not active.workspace_dir:
        return None
    return Path(active.workspace_dir).expanduser().resolve(strict=False)


def _candidate_strings(
    resolved: Path,
    original_path: str,
    workspace: Path | None,
    *,
    as_directory: bool = False,
) -> tuple[str, ...]:
    candidates: list[str] = [
        original_path.replace("\\", "/").lstrip("./"),
        resolved.as_posix(),
    ]
    if workspace is not None:
        try:
            relative = resolved.relative_to(workspace).as_posix()
        except ValueError:
            relative = ""
        if relative:
            candidates.extend([relative, f"./{relative}"])
    if as_directory:
        # A directory operand mutates everything beneath it; the trailing
        # slash lets dir/** style globs match the directory itself.
        candidates = [f"{candidate.rstrip('/')}/" for candidate in candidates]
    return tuple(dict.fromkeys(candidates))


def match_workspace_write_deny(
    path: Path,
    *,
    original_path: str | None = None,
    workspace: Path | None = None,
    ctx: ToolContext | None = None,
    as_directory: bool = False,
) -> WorkspaceWriteDenyMatch | None:
    """Return the deny rule matching a write target, if any.

    Patterns are opt-in and intentionally match both the original spelling and
    the active-workspace-relative path when a workspace is available.
    """

    from opensquilla.tools.run_mode import full_host_access_active

    if full_host_access_active():
        return None
    patterns = _workspace_write_deny_globs(ctx)
    if not patterns:
        return None
    resolved = path.expanduser().resolve(strict=False)
    workspace = workspace if workspace is not None else _workspace_root(ctx)
    original = original_path if original_path is not None else str(path)
    if workspace is not None:
        try:
            resolved.relative_to(workspace)
        except ValueError:
            # A workspace-internal spelling can resolve outside the workspace
            # when a path component is a symlink. The symlink guard keeps
            # matching against the lexical (non-resolved) view so a protected
            # name cannot be dodged by routing the write through a link.
            if not workspace_write_deny_symlink_guard():
                return None
            lexical = _lexical_workspace_path(original, workspace)
            if lexical is None:
                return None
            resolved = lexical
    candidates = _candidate_strings(resolved, original, workspace, as_directory=as_directory)

    for pattern in patterns:
        normalized_pattern = pattern.replace("\\", "/").lstrip("./")
        for candidate in candidates:
            normalized_candidate = candidate.replace("\\", "/").lstrip("./")
            if fnmatchcase(normalized_candidate, normalized_pattern) or fnmatchcase(
                f"/{normalized_candidate}", normalized_pattern
            ):
                if (
                    workspace is not None
                    and workspace_write_deny_tracked_only()
                    and not _workspace_path_is_git_tracked(
                        resolved, workspace, as_directory=as_directory
                    )
                ):
                    # Tracked-only mode: deny globs protect files under
                    # version control; files the agent created itself stay
                    # writable. Tracked-ness is a property of the path, so
                    # one untracked verdict settles every pattern.
                    return None
                return WorkspaceWriteDenyMatch(
                    pattern=pattern,
                    path=original,
                    resolved_path=str(resolved),
                )
    return None


def _lexical_workspace_path(original: str, workspace: Path) -> Path | None:
    """Lexical (symlink-free) workspace view of a path spelling, or None.

    normpath collapses ``..`` without resolving symlinks, so ``tests/link``
    keeps its workspace spelling even when the link target lives elsewhere.
    Relative spellings are joined against the workspace root, which matches
    how the shell and filesystem tools run in practice.
    """

    raw = os.path.expanduser(original.replace("\\", "/"))
    if not os.path.isabs(raw):
        raw = os.path.join(str(workspace), raw)
    lexical = Path(os.path.normpath(raw))
    try:
        lexical.relative_to(workspace)
    except ValueError:
        return None
    return lexical


def _workspace_path_is_git_tracked(
    resolved: Path, workspace: Path, *, as_directory: bool = False
) -> bool:
    """Whether git tracks the path; lookup failures fail closed (tracked).

    Tracked-only mode narrows deny enforcement to files under version
    control, so anything other than an authoritative "untracked" answer from
    git must not widen the allow set.
    """

    try:
        relative = resolved.relative_to(workspace).as_posix()
    except ValueError:
        return True
    if not relative or relative == ".":
        return True
    if as_directory:
        completed = run_git(
            ("ls-files", "--", f"{relative}/"),
            cwd=workspace,
            timeout=2.0,
        )
        if not completed.ok:
            return True
        return bool(completed.stdout.strip())
    completed = run_git(
        ("ls-files", "--error-unmatch", "--", relative),
        cwd=workspace,
        timeout=2.0,
    )
    if completed.state in {
        GitRunState.UNAVAILABLE,
        GitRunState.NOT_REPOSITORY,
        GitRunState.TIMED_OUT,
    }:
        return True
    if completed.ok:
        return True
    # --error-unmatch exits 1 for a valid repo with no matching tracked file;
    # any other status (e.g. 128 outside a repo) is a lookup failure.
    return completed.returncode != 1


def match_workspace_scratch_artifact(
    path: Path,
    *,
    original_path: str | None = None,
    workspace: Path | None = None,
    ctx: ToolContext | None = None,
) -> WorkspaceScratchArtifactMatch | None:
    """Return a match for new root diagnostic artifacts that belong in scratch.

    The check is intentionally narrow: it only applies when a scratch directory
    is configured, only for new root-level files inside the workspace, and never
    for paths already under the scratch directory.
    """

    from opensquilla.tools.run_mode import full_host_access_active

    if full_host_access_active():
        return None
    active = ctx if ctx is not None else current_tool_context.get()
    if active is None or not getattr(active, "scratch_dir", None):
        return None
    workspace = workspace if workspace is not None else _workspace_root(active)
    if workspace is None:
        return None
    resolved = path.expanduser().resolve(strict=False)
    scratch = Path(active.scratch_dir).expanduser().resolve(strict=False)  # type: ignore[arg-type]
    try:
        resolved.relative_to(scratch)
        return None
    except ValueError:
        pass
    try:
        relative = resolved.relative_to(workspace).as_posix()
    except ValueError:
        return None
    if "/" in relative or resolved.exists():
        return None
    if not any(pattern.match(relative) for pattern in _ROOT_DIAGNOSTIC_ARTIFACT_PATTERNS):
        return None
    original = original_path if original_path is not None else str(path)
    return WorkspaceScratchArtifactMatch(
        path=original,
        resolved_path=str(resolved),
        scratch_dir=str(scratch),
    )


def workspace_scratch_artifact_block(
    tool_name: str,
    match: WorkspaceScratchArtifactMatch,
    *,
    command: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "blocked",
        "reason": "workspace_scratch_artifact",
        "tool": tool_name,
        "path": match.path,
        "resolved_path": match.resolved_path,
        "scratch_dir": match.scratch_dir,
        "message": (
            f"{tool_name} blocked creation of a temporary diagnostic artifact in "
            f"the workspace root: {match.path}. Temporary reproduction, debug, "
            "verification, or candidate-patch files must be written under the "
            f"configured scratch directory instead: {match.scratch_dir}."
        ),
        "retryable": True,
    }
    if command is not None:
        payload["command"] = command
        payload["target"] = match.path
    return payload


def gate_workspace_scratch_artifact(
    tool_name: str,
    path: Path,
    *,
    original_path: str | None = None,
    workspace: Path | None = None,
) -> None:
    match = match_workspace_scratch_artifact(
        path,
        original_path=original_path,
        workspace=workspace,
    )
    if match is None:
        return
    # The mark lets opensquilla.tools.envelope apply the policy-deny
    # user_message cap override to this error; str(error) is unaffected.
    error = SafeToolError(str(workspace_scratch_artifact_block(tool_name, match)["message"]))
    error.policy_gate_denial = True
    raise error


def workspace_write_deny_block(
    tool_name: str,
    match: WorkspaceWriteDenyMatch,
    *,
    command: str | None = None,
) -> dict[str, object]:
    guidance = _deny_retry_guidance()
    guidance += _verify_mirror_guidance(match)
    payload: dict[str, object] = {
        "status": "blocked",
        "reason": "workspace_write_deny",
        "tool": tool_name,
        "path": match.path,
        "resolved_path": match.resolved_path,
        "matched_pattern": match.pattern,
        "message": (
            f"{tool_name} blocked by workspace write deny policy: "
            f"{match.path} matches {match.pattern}.{guidance}"
        ),
        "retryable": False,
    }
    if command is not None:
        payload["command"] = command
        payload["target"] = match.path
    return payload


def gate_workspace_write_deny(
    tool_name: str,
    path: Path,
    *,
    original_path: str | None = None,
    workspace: Path | None = None,
) -> None:
    match = match_workspace_write_deny(path, original_path=original_path, workspace=workspace)
    if match is None:
        return
    error = SafeToolError(str(workspace_write_deny_block(tool_name, match)["message"]))
    error.policy_gate_denial = True
    raise error


def _deny_retry_guidance(ctx: ToolContext | None = None) -> str:
    # Opt-in override for the remediation sentence appended to deny messages.
    # The scratch-dir guidance below tells the model to recreate the file in
    # scratch, which is the wrong instruction when deny globs protect files
    # that must not be modified or copied at all (e.g. test files); deployments
    # using deny globs that way can supply intent-appropriate wording here.
    override = os.environ.get("OPENSQUILLA_WORKSPACE_WRITE_DENY_GUIDANCE", "").strip()
    if override:
        return f" {override}"
    return _scratch_retry_guidance(ctx)


def _scratch_retry_guidance(ctx: ToolContext | None = None) -> str:
    active = ctx if ctx is not None else current_tool_context.get()
    scratch_dir = getattr(active, "scratch_dir", None) if active is not None else None
    if not scratch_dir:
        return ""
    return (
        " Temporary reproduction, debug, verification, or candidate-patch files "
        f"must be written under the configured scratch directory instead: {scratch_dir}."
    )


def verify_mirror_path(
    match_path: str, resolved_path: str, ctx: ToolContext | None = None
) -> str | None:
    """Writable mirror path for a deny-blocked workspace file, or None.

    Mirrors live under ``<scratch_dir>/verify-mirror/<workspace-relative-path>``
    so a denied in-package test edit can still be exercised in scratch. Only
    resolvable when a scratch directory and a workspace root are both
    configured and the target sits inside the workspace.
    """

    active = ctx if ctx is not None else current_tool_context.get()
    scratch_dir = getattr(active, "scratch_dir", None) if active is not None else None
    if not scratch_dir:
        return None
    workspace = _workspace_root(active)
    if workspace is None:
        return None
    try:
        relative = (
            Path(resolved_path).expanduser().resolve(strict=False).relative_to(workspace)
        ).as_posix()
    except ValueError:
        return None
    if not relative:
        return None
    scratch = Path(scratch_dir).expanduser().resolve(strict=False)
    return (scratch / "verify-mirror" / relative).as_posix()


def _verify_mirror_guidance(
    match: WorkspaceWriteDenyMatch, ctx: ToolContext | None = None
) -> str:
    active = ctx if ctx is not None else current_tool_context.get()
    if active is None or not getattr(active, "scratch_verify_mirror_active", False):
        return ""
    mirror = verify_mirror_path(match.path, match.resolved_path, active)
    if not mirror:
        return ""
    return (
        f" To exercise this file's checks without modifying it, copy it to the "
        f"writable mirror {mirror} first, keep the mirror copy identical to the "
        "workspace original, and add any new checks as separate files under the "
        "same verify-mirror directory."
    )
