"""Process-local authority for bounded authoring in ordinary channel turns."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opensquilla.safety.tool_tiers import WORKSPACE_AUTHORING_TOOLS
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionEntry,
    FileSystemPermissionProfile,
)
from opensquilla.sandbox.run_context import RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.tools.types import CallerKind, WorkspaceAccessError

if TYPE_CHECKING:
    from opensquilla.tools.types import ToolContext


@dataclass(frozen=True)
class WorkspaceAuthoringAttestation:
    """Turn-local proof; never hydrate this object from persisted metadata."""

    workspace: Path
    workspace_identity: tuple[int, int]
    parent_identity: tuple[int, int]
    session_key: str
    session_id: str | None
    runtime: Any = field(repr=False, compare=False)
    backend: Any = field(repr=False, compare=False)
    file_system: FileSystemPermissionProfile


def restricted_channel_context(ctx: ToolContext | None) -> bool:
    return bool(
        ctx is not None and ctx.caller_kind is CallerKind.CHANNEL and not ctx.channel_admin_verified
    )


def _workspace_identities(workspace: Path) -> tuple[tuple[int, int], tuple[int, int]]:
    if workspace != workspace.resolve(strict=True):
        raise ValueError("workspace is not canonical")
    for path in (workspace, workspace.parent):
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or path.is_symlink()
            or getattr(path, "is_junction", lambda: False)()
        ):
            raise ValueError("workspace directory changed")
        if os.name != "nt" and (
            info.st_uid != os.getuid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ValueError("workspace is writable by another principal")
    root = workspace.stat()
    parent = workspace.parent.stat()
    return (root.st_dev, root.st_ino), (parent.st_dev, parent.st_ino)


def attest_channel_workspace(
    ctx: ToolContext,
    *,
    binding_kind: str | None,
    binding_root: str | None,
    fresh: bool,
) -> None:
    """Freeze the gateway's managed-workspace capability, or leave it absent.

    ``binding_kind`` comes from the runtime-only session resolution path.
    Generic metadata, saved RunContext JSON, and model inputs cannot populate
    it. Windows cannot currently enforce the equivalent untrusted-process
    read boundary, so it remains fail-closed until that backend supports it.
    """

    ctx.sandboxed_workspace_authoring = None
    if not restricted_channel_context(ctx) or not fresh or binding_kind != "managed":
        return
    context = ctx.sandbox_run_context
    if (
        not isinstance(context, RunContext)
        or ctx.run_mode != RunMode.SAFE.value
        or context.run_mode is not RunMode.SAFE
        or context.mounts
        or ctx.sandbox_mounts
        or ctx.elevated
        or not ctx.workspace_dir
        or binding_root != ctx.workspace_dir
        or context.workspace != ctx.workspace_dir
        or not ctx.workspace_strict
        or not ctx.session_key
    ):
        return
    from opensquilla.sandbox.backend import BubblewrapBackend, SeatbeltBackend
    from opensquilla.sandbox.integration import get_runtime

    runtime = get_runtime()
    if (
        runtime is None
        or not runtime.effective.sandbox_enabled
        or not isinstance(runtime.backend, (BubblewrapBackend, SeatbeltBackend))
        or runtime.settings.extra_ro_mounts
        or runtime.settings.extra_rw_mounts
    ):
        return
    backend = runtime.backend
    try:
        # Backend.run() is the process contract. Both concrete backends above
        # enforce it; operation_domains_supported() only describes additional
        # non-process operations, so filesystem must be checked separately.
        if not backend.available() or "filesystem" not in backend.operation_domains_supported():
            return
        workspace = Path(ctx.workspace_dir)
        identity, parent_identity = _workspace_identities(workspace)
        profile = FileSystemPermissionProfile.workspace(
            workspace=workspace,
            host_root_readonly=False,
            tmp_writable=False,
            tmpdir_env_writable=False,
            denied_read_roots=tuple(Path(path) for path in runtime.settings.denied_read_roots),
            denied_read_globs=tuple(runtime.settings.denied_read_globs),
        )
    except (OSError, RuntimeError, ValueError):
        return
    ctx.workspace_lockdown = True
    ctx.scratch_dir = None
    ctx.environment = None
    ctx.sandboxed_workspace_authoring = WorkspaceAuthoringAttestation(
        workspace=workspace,
        workspace_identity=identity,
        parent_identity=parent_identity,
        session_key=ctx.session_key,
        session_id=ctx.session_id,
        runtime=runtime,
        backend=backend,
        file_system=profile,
    )


def workspace_authoring_attested(ctx: ToolContext | None) -> bool:
    """Validate the frozen authority against live execution facts, fail closed."""

    if not restricted_channel_context(ctx):
        return False
    assert ctx is not None
    proof = ctx.sandboxed_workspace_authoring
    if not isinstance(proof, WorkspaceAuthoringAttestation):
        return False
    context = ctx.sandbox_run_context
    if (
        ctx.session_key != proof.session_key
        or ctx.session_id != proof.session_id
        or ctx.workspace_dir != str(proof.workspace)
        or not ctx.workspace_strict
        or not ctx.workspace_lockdown
        or ctx.scratch_dir is not None
        or ctx.run_mode != RunMode.SAFE.value
        or ctx.elevated
        or ctx.sandbox_mounts
        or not isinstance(context, RunContext)
        or context.run_mode is not RunMode.SAFE
        or context.workspace != str(proof.workspace)
        or context.mounts
    ):
        return False
    from opensquilla.sandbox.integration import get_runtime

    runtime = get_runtime()
    if (
        runtime is not proof.runtime
        or not runtime.effective.sandbox_enabled
        or runtime.backend is not proof.backend
        or runtime.settings.extra_ro_mounts
        or runtime.settings.extra_rw_mounts
    ):
        return False
    try:
        # Readiness was proved once when this turn was built. In particular,
        # Bubblewrap.available() runs a namespace probe; repeating it for each
        # schema entry would turn catalog construction into hundreds of child
        # processes. Actual backend operations still fail closed on failure.
        return _workspace_identities(proof.workspace) == (
            proof.workspace_identity,
            proof.parent_identity,
        )
    except (OSError, RuntimeError, ValueError):
        return False


def require_workspace_authoring(ctx: ToolContext | None) -> WorkspaceAuthoringAttestation:
    if not workspace_authoring_attested(ctx):
        raise WorkspaceAccessError("Channel workspace authoring is unavailable.")
    assert ctx is not None
    proof = ctx.sandboxed_workspace_authoring
    assert isinstance(proof, WorkspaceAuthoringAttestation)
    return proof


def guard_channel_workspace_path(ctx: ToolContext | None, path: Path) -> None:
    """Enforce the bounded path before any filesystem handler touches it."""

    if not restricted_channel_context(ctx):
        return
    proof = require_workspace_authoring(ctx)
    try:
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(proof.workspace):
            raise ValueError("outside workspace")
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceAccessError("Channel files must remain in the task workspace.") from exc


def channel_workspace_file_system(
    ctx: ToolContext | None,
    *,
    readable_roots: tuple[Path, ...] = (),
) -> FileSystemPermissionProfile:
    """Intersect the bounded profile with the pinned operator restrictions.

    Safe policy normally has host-wide defaults. Only its restrictions may
    affect authoring, and a read-only carveout must never grant a host read.
    """
    from opensquilla.sandbox.file_policy import (
        authority_roots_for_state,
        compile_safe_file_profile,
    )
    from opensquilla.sandbox.policy_models import SandboxPolicy

    proof = require_workspace_authoring(ctx)
    assert ctx is not None
    runtime_profile = FileSystemPermissionProfile.read_only(
        readable_roots=readable_roots,
        host_root_readonly=False,
    )
    entries = [*runtime_profile.entries, *proof.file_system.entries]
    stored_policy = ctx.sandbox_policy
    if isinstance(stored_policy, SandboxPolicy):
        config = ctx.sandbox_gateway_config
        state_dir = str(getattr(config, "state_dir", "") or "").strip()
        restrictions = compile_safe_file_profile(
            stored_policy,
            authority_roots=authority_roots_for_state(state_dir) if state_dir else (),
            writable_roots=(proof.workspace,),
        )
        for entry in restrictions.entries:
            if entry.access is FileSystemAccess.DENY:
                entries.append(entry)
            elif entry.access is FileSystemAccess.READ:
                if entry.path.is_relative_to(proof.workspace):
                    entries.append(entry)
                elif proof.workspace.is_relative_to(entry.path):
                    entries.append(
                        FileSystemPermissionEntry(proof.workspace, FileSystemAccess.READ)
                    )
    denied = tuple(entry for entry in entries if entry.access is FileSystemAccess.DENY)
    denied_profile = FileSystemPermissionProfile(entries=denied)
    # Permission profiles otherwise choose the most specific declaration,
    # and a repeated declaration may replace an earlier one. Intersect every
    # grant with explicit denies before compiling: a nested READ/WRITE must
    # never reopen a denied ancestor, including canonical/lexical aliases.
    grants = tuple(
        entry
        for entry in entries
        if entry.access is not FileSystemAccess.DENY
        and not any(
            denied_profile.is_explicitly_denied(path) for path in (entry.path, entry.lexical_path)
        )
    )
    return FileSystemPermissionProfile(
        entries=(*grants, *denied),
        denied_read_globs=proof.file_system.denied_read_globs,
    )


__all__ = [
    "WORKSPACE_AUTHORING_TOOLS",
    "WorkspaceAuthoringAttestation",
    "attest_channel_workspace",
    "channel_workspace_file_system",
    "guard_channel_workspace_path",
    "require_workspace_authoring",
    "restricted_channel_context",
    "workspace_authoring_attested",
]
