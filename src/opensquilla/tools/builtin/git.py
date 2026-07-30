"""Git built-in tools: git_status, git_diff, git_commit, git_log."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

from opensquilla.sandbox.integration import get_runtime, run_under_backend
from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionProfile,
)
from opensquilla.sandbox.policy import build_policy, select_level
from opensquilla.tools.path_policy import reject_foreign_host_path
from opensquilla.tools.registry import tool
from opensquilla.tools.run_mode import full_host_access_active
from opensquilla.tools.types import current_tool_context
from opensquilla.tools.write_tracking import summarize_patch_hygiene_warning


def _effective_workdir(workdir: str | None) -> str | None:
    ctx = current_tool_context.get()
    if workdir:
        workspace = (
            Path(ctx.workspace_dir).expanduser().resolve(strict=False)
            if ctx is not None and ctx.workspace_dir
            else None
        )
        reject_foreign_host_path(workdir, platform=os.name, workspace=workspace)
        return workdir
    if ctx and ctx.workspace_dir:
        return str(Path(ctx.workspace_dir).expanduser().resolve())
    return None


def _reject_foreign_git_path(path: str) -> None:
    ctx = current_tool_context.get()
    workspace = (
        Path(ctx.workspace_dir).expanduser().resolve(strict=False)
        if ctx is not None and ctx.workspace_dir
        else None
    )
    reject_foreign_host_path(path, platform=os.name, workspace=workspace)


async def _run_git(*args: str, cwd: str | None = None) -> str:
    runtime = get_runtime()
    if (
        runtime is not None
        and runtime.effective.sandbox_enabled
        and not full_host_access_active()
    ):
        ctx = current_tool_context.get()
        if cwd:
            workspace = Path(cwd).expanduser().resolve()
        elif ctx and ctx.workspace_dir:
            workspace = Path(ctx.workspace_dir).expanduser().resolve()
        else:
            workspace = runtime.workspace.expanduser().resolve()
        action_kind = (
            "git.write" if any(arg in {"add", "commit"} for arg in args[:2]) else "git.read"
        )
        level = (
            select_level(action_kind)
            if runtime.effective.grading_enabled
            else runtime.effective.default_level
        )
        policy = build_policy(
            level,
            action_kind,
            workspace,
            runtime.settings,
            trusted=True,
        )
        request_args = args
        profile = (
            ctx.sandbox_file_system_profile if ctx is not None else None
        )
        if isinstance(profile, FileSystemPermissionProfile):
            read_only = not any(
                entry.access is FileSystemAccess.WRITE for entry in profile.entries
            )
            policy = replace(policy, file_system=profile)
            if read_only:
                policy = replace(
                    policy,
                    mounts=tuple(mount.with_mode("ro") for mount in policy.mounts),
                    workspace_rw=False,
                    tmp_writable=False,
                )
                request_args = _harden_read_only_git_args(args)
        result = await run_under_backend(
            build_request_for_git(request_args, workspace, action_kind, policy),
            runtime=runtime,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed (exit {result.returncode}):\n{output}")
        return output
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )
    stdout, _ = await proc.communicate()
    from opensquilla.subprocess_encoding import decode_subprocess_output

    output = decode_subprocess_output(stdout)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed (exit {proc.returncode}):\n{output}")
    return output


def _harden_read_only_git_args(args: tuple[str, ...]) -> tuple[str, ...]:
    """Disable repository-controlled helpers for read-only git execution."""

    global_options = ("--no-optional-locks", "-c", "core.fsmonitor=false")
    if args and args[0] == "diff":
        return (
            *global_options,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            *args[1:],
        )
    return (*global_options, *args)


def build_request_for_git(args: tuple[str, ...], cwd: Path, action_kind: str, policy):
    from opensquilla.sandbox.integration import build_request

    return build_request(
        action_kind=action_kind,
        argv=("git", *args),
        cwd=cwd,
        policy=policy,
        env={},
    )


def _append_patch_hygiene_warning(output: str, paths: list[str], cwd: str | None) -> str:
    if not paths or cwd is None:
        return output
    repo = Path(cwd).expanduser().resolve(strict=False)
    resolved_paths = [
        path if path.is_absolute() else repo / path
        for raw in paths
        if (path := Path(raw.replace("\\", "/")))
    ]
    warning = summarize_patch_hygiene_warning(resolved_paths)
    if not warning:
        return output
    return f"{warning}\n{output}"


def _diff_paths(output: str, explicit_path: str | None = None) -> list[str]:
    if explicit_path:
        return [explicit_path]
    paths: list[str] = []
    for line in output.splitlines():
        if not line.startswith("diff --git "):
            continue
        try:
            paths.append(line.split(" b/", 1)[1])
        except IndexError:
            continue
    return paths


def _status_paths(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines():
        if not line or line.startswith("##") or len(line) < 4:
            continue
        raw = line[3:].strip()
        if " -> " in raw:
            raw = raw.rsplit(" -> ", 1)[1]
        if raw:
            paths.append(raw)
    return paths


@tool(
    name="git_status",
    description="Show the working tree status.",
    params={
        "workdir": {"type": "string", "description": "Git repository directory (default: cwd)."},
    },
    required=[],
    sandbox=SandboxToolDescriptor.process(
        kind="git.read",
        argv_factory=lambda a: ("git", "status", "--short", "--branch"),
        record_payload=False,
    ),
)
async def git_status(workdir: str | None = None) -> str:
    cwd = _effective_workdir(workdir)
    output = await _run_git("status", "--short", "--branch", cwd=cwd)
    return _append_patch_hygiene_warning(output, _status_paths(output), cwd)


@tool(
    name="git_diff",
    description="Show git diff (staged + unstaged changes).",
    params={
        "path": {"type": "string", "description": "Limit diff to this path."},
        "staged": {"type": "boolean", "description": "Show only staged changes."},
        "workdir": {"type": "string", "description": "Git repository directory (default: cwd)."},
    },
    required=[],
    sandbox=SandboxToolDescriptor.process(
        kind="git.read",
        argv_factory=lambda a: (
            "git",
            "diff",
            "--cached" if a.get("staged") else "--unstaged",
            str(a.get("path", "")),
        ),
        record_payload=False,
    ),
)
async def git_diff(
    path: str | None = None,
    staged: bool = False,
    workdir: str | None = None,
) -> str:
    args = ["diff"]
    if staged:
        args.append("--cached")
    if path:
        _reject_foreign_git_path(path)
        args += ["--", path]
    cwd = _effective_workdir(workdir)
    output = await _run_git(*args, cwd=cwd)
    return _append_patch_hygiene_warning(output, _diff_paths(output, path), cwd)


@tool(
    name="git_commit",
    description="Stage specified files (or all changes) and create a commit.",
    params={
        "message": {"type": "string", "description": "Commit message."},
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Files to stage. If omitted, stages all changes (git add -A).",
        },
        "workdir": {"type": "string", "description": "Git repository directory (default: cwd)."},
    },
    required=["message"],
    owner_only=True,
    sandbox=SandboxToolDescriptor.process(
        kind="git.write",
        argv_factory=lambda a: (
            "git",
            "commit",
            str(a.get("message", "")),
            str(len(a.get("files") or [])),
        ),
        record_payload=False,
    ),
)
async def git_commit(
    message: str,
    files: list[str] | None = None,
    workdir: str | None = None,
) -> str:
    cwd = _effective_workdir(workdir)
    if files:
        for file_path in files:
            _reject_foreign_git_path(file_path)
        await _run_git("add", "--", *files, cwd=cwd)
    else:
        await _run_git("add", "-A", cwd=cwd)
    return await _run_git("commit", "-m", message, cwd=cwd)


@tool(
    name="git_log",
    description="Show recent git commit log.",
    params={
        "count": {"type": "integer", "description": "Number of commits to show (default 10)."},
        "workdir": {"type": "string", "description": "Git repository directory (default: cwd)."},
    },
    required=[],
    sandbox=SandboxToolDescriptor.process(
        kind="git.read",
        argv_factory=lambda a: ("git", "log", str(a.get("count", 10))),
        record_payload=False,
    ),
)
async def git_log(count: int = 10, workdir: str | None = None) -> str:
    return await _run_git(
        "log",
        f"--max-count={count}",
        "--oneline",
        "--decorate",
        cwd=_effective_workdir(workdir),
    )
