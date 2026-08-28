"""Level selection and policy construction.

Two pure functions form the public surface:

* :func:`select_level` — a deterministic rule table mapping an ``action_kind``
  and a small set of boolean hints onto a :class:`SecurityLevel`.
* :func:`build_policy` — turns a level + workspace + settings into a fully
  materialised :class:`SandboxPolicy`, resolving mounts, network mode,
  resource caps and the approval flag.

Keeping both pure makes them trivial to unit-test and means the integration
layer can call them without side effects.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.sandbox.platform_permissions import current_platform_context
from opensquilla.sandbox.types import (
    SANDBOX_WORKSPACE_PATH,
    MountSpec,
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SecurityLevel,
)

_DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "HOME",
    "LANG",
    "LC_ALL",
    "TERM",
    "SHELL",
    "USER",
    "LOGNAME",
    "HOSTNAME",
    "PWD",
    "WORKSPACE_DIR",
    "PROJECT_ROOT",
    "COMSPEC",
    "SystemRoot",
    "SYSTEMROOT",
    "WINDIR",
    "USERPROFILE",
    "TEMP",
    "TMP",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "PSModulePath",
)


@dataclass(frozen=True)
class LevelHints:
    """Inputs to :func:`select_level` beyond the action tag.

    Each hint is a small, auditable boolean. This is intentionally a flat
    dataclass rather than a free-form dict so the rule table stays
    enumerable and every call site declares its own intent.
    """

    trusted_source: bool = True
    needs_network: bool = False
    writes_outside_workspace: bool = False
    crosses_trust_boundary: bool = False
    high_impact: bool = False


_NETWORK_TAGS: frozenset[str] = frozenset({"network.fetch", "network.http", "web.fetch"})
_FS_READ_TAGS: frozenset[str] = frozenset({"fs.read", "fs.list", "fs.grep"})
_FS_WRITE_TAGS: frozenset[str] = frozenset({"fs.write", "fs.edit", "patch.apply"})
_CODE_TAGS: frozenset[str] = frozenset({"code.exec", "shell.exec", "shell.background"})
_GIT_TAGS: frozenset[str] = frozenset({"git.read", "git.write"})


def select_level(action_kind: str, hints: LevelHints | None = None) -> SecurityLevel:
    """Pick a :class:`SecurityLevel` from the action kind + hints.

    The rule table is deterministic and favours escalation:

    * any untrusted-source action starts at ``STRICT``; trust-boundary
      crossings escalate to ``LOCKED``.
    * plain FS reads stay at ``STANDARD`` — the sandbox still engages for
      isolation but no approval is required.
    * FS writes and git writes land at ``STANDARD`` by default, but promote
      to ``STRICT`` if they leave the workspace.
    * code / shell execution starts at ``STANDARD`` when the source is
      trusted and at ``STRICT`` otherwise; high-impact flags push to
      ``LOCKED``.
    * network egress sits at ``STANDARD`` (so the policy layer can still
      choose ``network=NONE`` in strict mode); untrusted or
      boundary-crossing network calls jump to ``LOCKED``.

    The function never returns ``DISABLED`` — that level is reachable only
    through explicit configuration, not action-level inference.
    """
    h = hints or LevelHints()
    if action_kind in _FS_READ_TAGS:
        return SecurityLevel.STANDARD

    if action_kind in _FS_WRITE_TAGS or action_kind in _GIT_TAGS:
        if h.writes_outside_workspace or h.crosses_trust_boundary:
            return SecurityLevel.STRICT
        return SecurityLevel.STANDARD

    if action_kind in _CODE_TAGS:
        if h.high_impact or h.crosses_trust_boundary:
            return SecurityLevel.LOCKED
        if not h.trusted_source:
            return SecurityLevel.STRICT
        return SecurityLevel.STANDARD

    if action_kind in _NETWORK_TAGS:
        if not h.trusted_source or h.crosses_trust_boundary:
            return SecurityLevel.LOCKED
        return SecurityLevel.STANDARD

    if not h.trusted_source:
        return SecurityLevel.STRICT
    if h.high_impact:
        return SecurityLevel.LOCKED
    return SecurityLevel.STANDARD


def _resolve_limits(level: SecurityLevel, settings: SandboxSettings) -> ResourceLimits:
    base_cpu = max(1, settings.cpu_seconds)
    base_mem = max(64, settings.memory_mb)
    base_wall = float(max(1, settings.wall_seconds))
    if level == SecurityLevel.STRICT:
        return ResourceLimits(
            cpu_seconds=max(1, base_cpu // 2),
            memory_mb=max(128, base_mem // 2),
            pids=128,
            wall_timeout_s=max(5.0, base_wall / 2),
        )
    if level == SecurityLevel.LOCKED:
        return ResourceLimits(
            cpu_seconds=max(1, base_cpu // 3),
            memory_mb=max(64, base_mem // 4),
            pids=64,
            wall_timeout_s=max(5.0, base_wall / 3),
        )
    if level == SecurityLevel.DISABLED:
        return ResourceLimits(
            cpu_seconds=base_cpu * 10,
            memory_mb=base_mem * 4,
            pids=1024,
            wall_timeout_s=base_wall * 10,
        )
    return ResourceLimits(
        cpu_seconds=base_cpu,
        memory_mb=base_mem,
        pids=256,
        wall_timeout_s=base_wall,
    )


def _resolve_network(
    level: SecurityLevel,
    action_kind: str,
    settings: SandboxSettings,
    hints: LevelHints | None = None,
) -> NetworkMode:
    if level == SecurityLevel.DISABLED:
        return NetworkMode.HOST
    h = hints or LevelHints()
    if level == SecurityLevel.STANDARD and (
        action_kind in _NETWORK_TAGS or (action_kind in _CODE_TAGS and h.needs_network)
    ):
        if settings.network_default == "proxy_allowlist":
            return NetworkMode.PROXY_ALLOWLIST
        return NetworkMode.NONE
    return NetworkMode.NONE


def _collect_mounts(
    level: SecurityLevel,
    workspace: Path,
    settings: SandboxSettings,
    session_mounts: tuple[MountSpec, ...] = (),
) -> tuple[tuple[MountSpec, ...], bool]:
    """Build the ordered mount list.

    Returns ``(mounts, workspace_rw)``. The workspace always appears; extra
    mounts from settings come after so their precedence is unambiguous.
    """
    mounts: list[MountSpec] = []
    if sys.platform.startswith("linux") and settings.host_root_readonly:
        mounts.append(
            MountSpec(
                host_path=Path("/"),
                sandbox_path=Path("/"),
                mode="ro",
                required=True,
            )
        )
    workspace_rw = level != SecurityLevel.LOCKED
    mounts.append(
        MountSpec(
            host_path=workspace,
            sandbox_path=SANDBOX_WORKSPACE_PATH,
            mode="rw" if workspace_rw else "ro",
            required=True,
        )
    )
    if level in (SecurityLevel.STANDARD, SecurityLevel.DISABLED):
        mounts.extend(session_mounts)
    elif level == SecurityLevel.STRICT:
        mounts.extend(mount.with_mode("ro") for mount in session_mounts)
    if level in (SecurityLevel.STANDARD, SecurityLevel.DISABLED):
        for host in settings.extra_ro_mounts:
            p = Path(host)
            mounts.append(MountSpec(host_path=p, sandbox_path=p, mode="ro", required=False))
        for host in settings.extra_rw_mounts:
            p = Path(host)
            mounts.append(MountSpec(host_path=p, sandbox_path=p, mode="rw", required=False))
    elif level == SecurityLevel.STRICT:
        # Strict: extras are downgraded to ro regardless of declared mode so a
        # sloppy config can't silently widen write exposure.
        for host in (*settings.extra_ro_mounts, *settings.extra_rw_mounts):
            p = Path(host)
            mounts.append(MountSpec(host_path=p, sandbox_path=p, mode="ro", required=False))
    # LOCKED: no extras. Workspace only.
    return tuple(mounts), workspace_rw


def _describe(level: SecurityLevel, action_kind: str) -> str:
    return f"{level.label} policy for action {action_kind!r}"


def _configured_denied_read_roots(
    workspace: Path,
    settings: SandboxSettings,
) -> tuple[Path, ...]:
    roots: list[Path] = []
    for raw in settings.denied_read_roots:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = workspace / path
        roots.append(path.absolute())
    return tuple(roots)


def _configured_denied_read_globs(
    workspace: Path,
    settings: SandboxSettings,
) -> tuple[str, ...]:
    patterns: list[str] = []
    for raw in settings.denied_read_globs:
        pattern = Path(raw).expanduser()
        if not pattern.is_absolute():
            pattern = workspace / pattern
        patterns.append(pattern.as_posix())
    return tuple(patterns)


def build_policy(
    level: SecurityLevel,
    action_kind: str,
    workspace: Path,
    settings: SandboxSettings,
    *,
    trusted: bool = True,
    hints: LevelHints | None = None,
    session_mounts: tuple[MountSpec, ...] = (),
) -> SandboxPolicy:
    """Materialise a :class:`SandboxPolicy` for ``level``.

    ``trusted`` is kept as an explicit argument (rather than folded into the
    level) so callers can record it in logs alongside the chosen level. It
    does not alter the level here — callers are expected to have passed the
    flag through :func:`select_level` already — but it does force approval on
    for any untrusted action.

    ``workspace`` must be an absolute path. If the caller is unsure, resolve
    it first; we do not call :meth:`Path.resolve` here to avoid surprising
    symlink expansion.
    """
    if not workspace.is_absolute():
        raise ValueError(f"workspace must be an absolute path, got {workspace!r}")

    mounts, workspace_rw = _collect_mounts(level, workspace, settings, session_mounts)
    declared_writable_roots = tuple(mount.host_path for mount in mounts if mount.mode == "rw")
    platform_context = current_platform_context(
        cwd=workspace,
        writable_roots=declared_writable_roots,
    )
    limits = _resolve_limits(level, settings)
    network = _resolve_network(level, action_kind, settings, hints)
    tmp_writable = level != SecurityLevel.LOCKED
    denied_read_roots = _configured_denied_read_roots(workspace, settings)
    denied_read_globs = _configured_denied_read_globs(workspace, settings)

    require_approval = level >= SecurityLevel.STRICT and (
        level == SecurityLevel.LOCKED or not trusted
    )

    if level == SecurityLevel.DISABLED:
        file_system = FileSystemPermissionProfile.full_access()
    elif workspace_rw:
        readable_roots = tuple(mount.host_path for mount in mounts if mount.mode == "ro")
        writable_roots = tuple(
            mount.host_path
            for mount in mounts
            if mount.mode == "rw" and mount.host_path != workspace
        )
        file_system = FileSystemPermissionProfile.workspace(
            workspace=workspace,
            readable_roots=readable_roots,
            writable_roots=writable_roots,
            denied_read_roots=denied_read_roots,
            denied_read_globs=denied_read_globs,
            host_root_readonly=settings.host_root_readonly,
            tmp_writable=tmp_writable and not settings.exclude_slash_tmp,
            tmpdir_env_writable=(tmp_writable and not settings.exclude_tmpdir_env_var),
            platform_context=platform_context,
        )
    else:
        readonly_mount_roots = [mount.host_path for mount in mounts]
        file_system = FileSystemPermissionProfile.read_only(
            readable_roots=readonly_mount_roots,
            denied_read_roots=denied_read_roots,
            denied_read_globs=denied_read_globs,
            host_root_readonly=settings.host_root_readonly,
            platform_context=platform_context,
        )

    return SandboxPolicy(
        level=level,
        network=network,
        mounts=mounts,
        workspace_rw=workspace_rw,
        tmp_writable=tmp_writable,
        limits=limits,
        env_allowlist=_DEFAULT_ENV_ALLOWLIST,
        require_approval=require_approval,
        description=_describe(level, action_kind),
        file_system=file_system,
    )


__all__ = [
    "LevelHints",
    "build_policy",
    "select_level",
]
