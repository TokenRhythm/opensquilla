"""Owner-only read-only Git inspection RPC (``git.status`` and ``git.diff``)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from opensquilla.gateway.adapters.git_contract import register_git_contract
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.git_runtime import GitRunResult, GitRunState, run_git

_d = get_dispatcher()
_GIT_TIMEOUT_SECONDS: Final = 5.0
_MAX_DIFF_CHARACTERS: Final = 200_000
_BINARY_DIFF_MARKERS: Final[tuple[str, ...]] = ("Binary files", "GIT binary patch")
_STATUS_LETTERS: Final[dict[str, str]] = {
    "M": "modified",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "added",
    "T": "modified",
}


def _require_owner(ctx: RpcContext) -> None:
    if not ctx.principal.is_owner:
        raise RpcHandlerError(
            "OWNER_REQUIRED",
            "Git inspection requires a locally proven owner.",
        )


def _params(params: dict | None) -> dict[str, Any]:
    """Return the raw params mapping; both handlers read it defensively."""

    return params if isinstance(params, dict) else {}


async def _resolve_workdir(params: dict[str, Any], ctx: RpcContext) -> str:
    """Resolve the requested project workspace path, or the process directory."""

    workspace_id = params.get("workspaceId")
    if isinstance(workspace_id, str) and workspace_id.strip():
        storage = get_session_storage(ctx.session_manager)
        workspace = (
            await storage.get_project_workspace(workspace_id.strip())
            if storage is not None
            else None
        )
        if workspace is None or workspace.removed_at is not None:
            raise RpcHandlerError("WORKSPACE_NOT_FOUND", "Project workspace not found.")
        candidate = Path(workspace.path)
    else:
        candidate = Path.cwd()
    resolved = candidate.expanduser()
    if not resolved.is_absolute():
        resolved = resolved.absolute()
    return str(resolved)


async def _git(args: Sequence[str], cwd: str) -> GitRunResult:
    """Run one bounded Git command off the event loop."""

    return await asyncio.to_thread(run_git, args, cwd, _GIT_TIMEOUT_SECONDS)


def _unavailable_status(reason: str) -> dict[str, Any]:
    """Return a fully populated status payload for an unusable repository."""

    return {
        "available": False,
        "workspacePath": None,
        "branch": None,
        "detached": False,
        "unborn": False,
        "headOid": None,
        "ahead": 0,
        "behind": 0,
        "clean": True,
        "files": [],
        "reason": reason,
    }


def _rev_count(result: GitRunResult) -> int:
    if result.state is not GitRunState.OK:
        return 0
    try:
        return max(int(result.stdout_text.strip()), 0)
    except ValueError:
        return 0


def _parse_status_listing(text: str) -> list[dict[str, Any]]:
    """Parse ``git status --porcelain=v1 -z`` output into Contract file entries."""

    files: list[dict[str, Any]] = []
    entries = text.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4 or entry[2] != " ":
            continue
        code = entry[:2]
        path = entry[3:]
        if not path:
            continue
        # In ``-z`` output a rename/copy entry is followed by its source path.
        previous_path: str | None = None
        if "R" in code or "C" in code:
            if index < len(entries) and entries[index]:
                previous_path = entries[index]
            index += 1
        if code == "??":
            files.append({"path": path, "status": "untracked", "staged": False})
            continue
        staged = code[0] not in {" ", "?"}
        if code in {"AA", "DD"} or "U" in code:
            status = "conflicted"
        else:
            status = _STATUS_LETTERS.get(code[0] if staged else code[1], "modified")
        payload: dict[str, Any] = {"path": path, "status": status, "staged": staged}
        if previous_path is not None:
            payload["previousPath"] = previous_path
        files.append(payload)
    return files


async def _handle_git_status(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    _require_owner(ctx)
    workdir = await _resolve_workdir(_params(params), ctx)

    probe = await _git(("rev-parse", "--is-inside-work-tree"), workdir)
    if probe.state is GitRunState.UNAVAILABLE:
        return {"status": _unavailable_status("git_unavailable")}
    if probe.state is not GitRunState.OK or probe.stdout_text.strip() != "true":
        return {"status": _unavailable_status("not_repository")}

    toplevel = await _git(("rev-parse", "--show-toplevel"), workdir)
    workspace_path: str | None = None
    if toplevel.state is GitRunState.OK:
        workspace_path = toplevel.stdout_text.strip() or None

    symbolic = await _git(("symbolic-ref", "--short", "HEAD"), workdir)
    branch: str | None = None
    if symbolic.state is GitRunState.OK:
        branch = symbolic.stdout_text.strip() or None

    head = await _git(("rev-parse", "HEAD"), workdir)
    head_oid: str | None = None
    if head.state is GitRunState.OK:
        head_oid = head.stdout_text.strip() or None

    ahead = _rev_count(await _git(("rev-list", "--count", "@{u}..HEAD"), workdir))
    behind = _rev_count(await _git(("rev-list", "--count", "HEAD..@{u}"), workdir))

    listing = await _git(
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        workdir,
    )
    files = _parse_status_listing(listing.stdout_text) if listing.state is GitRunState.OK else []
    return {
        "status": {
            "available": True,
            "workspacePath": workspace_path,
            "branch": branch,
            "detached": branch is None,
            "unborn": head_oid is None,
            "headOid": head_oid,
            "ahead": ahead,
            "behind": behind,
            "clean": not files,
            "files": files,
            "reason": None,
        }
    }


def _empty_diff(path: str) -> dict[str, Any]:
    """Return the empty diff shape used when Git cannot answer at all."""

    return {
        "path": path,
        "diff": "",
        "binary": False,
        "truncated": False,
        "additions": 0,
        "deletions": 0,
    }


def _diff_line_counts(text: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions


async def _handle_git_diff(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    _require_owner(ctx)
    values = _params(params)
    path = values.get("path")
    if not isinstance(path, str) or not path.strip():
        raise RpcHandlerError("INVALID_PARAMS", "path is required")
    staged = values.get("staged") is True
    workdir = await _resolve_workdir(values, ctx)

    args: list[str] = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
    if staged:
        args.append("--cached")
    args.extend(("--", path))

    result = await _git(tuple(args), workdir)
    if result.state in (GitRunState.UNAVAILABLE, GitRunState.NOT_REPOSITORY):
        return {"diff": _empty_diff(path)}

    text = result.stdout_text
    additions, deletions = _diff_line_counts(text)
    binary = any(marker in text for marker in _BINARY_DIFF_MARKERS)
    truncated = len(text) > _MAX_DIFF_CHARACTERS
    if truncated:
        text = text[:_MAX_DIFF_CHARACTERS]
    return {
        "diff": {
            "path": path,
            "diff": text,
            "binary": binary,
            "truncated": truncated,
            "additions": additions,
            "deletions": deletions,
        }
    }


_GIT_CONTRACT_IMPLEMENTATIONS = {
    "git.status": _handle_git_status,
    "git.diff": _handle_git_diff,
}

_GIT_CONTRACT_HANDLERS = {
    method: register_git_contract(
        _d,
        method,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
    for method, implementation in _GIT_CONTRACT_IMPLEMENTATIONS.items()
}


__all__ = [
    "_handle_git_diff",
    "_handle_git_status",
]
