"""Sandbox mount visibility checks for host paths."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Literal, cast

from opensquilla.sandbox.path_aliases import resolve_workspace_alias
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionProfile,
    logical_absolute_path,
)
from opensquilla.sandbox.sensitive_paths import sensitive_path_marker

MountAccess = Literal["ro", "rw"]
MountStatus = Literal["allowed", "request", "blocked"]
DecisionPath = Path | PurePosixPath | PureWindowsPath


@dataclass(frozen=True)
class MountDecision:
    status: MountStatus
    normalized_path: str
    access: MountAccess
    reason: str = ""


@dataclass(frozen=True)
class PathRiskClassification:
    normalized_path: str
    within_workspace: bool = False
    protected: bool = False
    low_risk_user_area: bool = False
    reason: str = ""


_POSIX_BLOCKED_PREFIXES: tuple[str, ...] = (
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/root",
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/private/var/run/docker.sock",
)

_POSIX_SYSTEM_WRITE_PREFIXES: tuple[str, ...] = (
    "/Applications",
    "/Library",
    "/System",
    "/bin",
    "/opt",
    "/sbin",
    "/usr",
)
_PROTECTED_METADATA_PARTS: frozenset[str] = frozenset(
    {
        ".aws",
        ".azure",
        ".codex",
        ".docker",
        ".git",
        ".gnupg",
        ".kube",
        ".ssh",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)


def normalize_mount_access(value: Any, default: MountAccess = "ro") -> MountAccess:
    return "rw" if isinstance(value, str) and value.lower().strip() == "rw" else default


def normalize_path(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def logical_tool_path(
    raw_path: str | os.PathLike[str],
    *,
    base: Path,
) -> Path:
    """Make a tool path absolute without following any symlink component."""

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    logical = logical_absolute_path(candidate)
    assert isinstance(logical, Path)
    return logical


def _profile_logical_candidate(
    logical_path: str | os.PathLike[str],
    *,
    workspace: str | os.PathLike[str] | None,
) -> DecisionPath:
    """Resolve a raw model-visible spelling without losing its platform form."""

    workspace_base = _profile_workspace_base(workspace)
    raw_candidate = _raw_profile_logical_path(logical_path, workspace_base)
    alias = resolve_workspace_alias(raw_candidate, workspace_base)
    if alias is not None:
        return _logical_path_from_base(
            cast(DecisionPath, alias),
            base=workspace_base or Path.cwd(),
        )
    return _logical_path_from_base(
        raw_candidate,
        base=workspace_base or Path.cwd(),
    )


def _profile_workspace_base(
    workspace: str | os.PathLike[str] | None,
) -> DecisionPath | None:
    if workspace is None:
        return None
    if isinstance(workspace, PurePath) and not isinstance(workspace, Path):
        return cast(DecisionPath, workspace)
    return logical_tool_path(workspace, base=Path.cwd())


def _raw_profile_logical_path(
    logical_path: str | os.PathLike[str],
    workspace_base: DecisionPath | None,
) -> DecisionPath:
    if isinstance(logical_path, PurePath):
        return cast(DecisionPath, logical_path)

    raw_text = os.fsdecode(os.fspath(logical_path))
    if isinstance(workspace_base, PureWindowsPath):
        if raw_text.startswith("/") and not raw_text.startswith("//"):
            return PurePosixPath(raw_text)
        return PureWindowsPath(raw_text)
    return Path(raw_text).expanduser()


def _logical_path_from_base(
    candidate: DecisionPath,
    *,
    base: DecisionPath,
) -> DecisionPath:
    if not candidate.is_absolute():
        candidate = base / candidate
    logical = logical_absolute_path(candidate)
    assert isinstance(logical, (Path, PurePosixPath, PureWindowsPath))
    return logical


def _looks_like_posix_rooted_text(path: str) -> bool:
    return os.name == "nt" and path.startswith("/") and not path.startswith("//")


def _normalize_decision_path(path: str | os.PathLike[str]) -> DecisionPath:
    if isinstance(path, str) and _looks_like_posix_rooted_text(path):
        return PurePosixPath(path)
    if isinstance(path, PurePath) and not isinstance(path, Path):
        return cast(DecisionPath, path)
    return normalize_path(path)


def is_relative_to_path(candidate: PurePath, root: PurePath) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def decide_path_access(
    path: str | os.PathLike[str],
    *,
    workspace: str | os.PathLike[str] | None,
    mounts: Iterable[Any] = (),
    write: bool = False,
    profile: FileSystemPermissionProfile | None = None,
    logical_path: str | os.PathLike[str] | None = None,
) -> MountDecision:
    """Return whether *path* is visible in the active sandbox mount view."""

    access: MountAccess = "rw" if write else "ro"
    normalized = _normalize_decision_path(path)
    normalized_text = str(normalized)
    workspace_path = _normalize_decision_path(workspace) if workspace is not None else None

    if profile is not None:
        profile_candidate = (
            _profile_logical_candidate(logical_path, workspace=workspace)
            if logical_path is not None
            else normalized
        )
        profile_candidates = tuple(dict.fromkeys((profile_candidate, normalized)))
        candidate_access = tuple(
            (candidate, profile.resolve(candidate)) for candidate in profile_candidates
        )
        effective_access = min(
            (candidate[1] for candidate in candidate_access),
            key={
                FileSystemAccess.DENY: 0,
                FileSystemAccess.READ: 1,
                FileSystemAccess.WRITE: 2,
            }.__getitem__,
        )
        if effective_access is FileSystemAccess.DENY:
            if not any(
                access is FileSystemAccess.DENY and profile.is_explicitly_denied(candidate)
                for candidate, access in candidate_access
            ):
                return MountDecision(
                    status="request",
                    normalized_path=normalized_text,
                    access=access,
                    reason="outside_sandbox_mounts",
                )
            return MountDecision(
                status="blocked",
                normalized_path=normalized_text,
                access=access,
                reason="denied_read",
            )
        if not write or effective_access is FileSystemAccess.WRITE:
            return MountDecision(
                status="allowed",
                normalized_path=normalized_text,
                access=access,
            )
        return MountDecision(
            status="request",
            normalized_path=normalized_text,
            access="rw",
            reason=(
                "protected_metadata"
                if any(
                    profile.protected_metadata_root(candidate) is not None
                    for candidate in profile_candidates
                )
                else "mount_requires_write_access"
            ),
        )

    if workspace_path is not None and is_relative_to_path(normalized, workspace_path):
        return MountDecision(
            status="allowed",
            normalized_path=normalized_text,
            access=access,
        )

    matching_mounts = [
        (mount_root, mount_access)
        for mount_root, mount_access in _iter_mount_roots(mounts)
        if is_relative_to_path(normalized, mount_root)
    ]
    if matching_mounts:
        _mount_root, mount_access = max(
            matching_mounts,
            key=lambda item: len(item[0].parts),
        )
        if not write or mount_access == "rw":
            return MountDecision(
                status="allowed",
                normalized_path=normalized_text,
                access=access,
            )
        return MountDecision(
            status="request",
            normalized_path=normalized_text,
            access="rw",
            reason="mount_requires_write_access",
        )

    return MountDecision(
        status="request",
        normalized_path=normalized_text,
        access=access,
        reason="outside_sandbox_mounts",
    )


def classify_path_for_sandbox(
    path: str | os.PathLike[str],
    *,
    workspace: str | os.PathLike[str] | None,
) -> PathRiskClassification:
    normalized = _normalize_decision_path(path)
    normalized_text = str(normalized)
    workspace_path = _normalize_decision_path(workspace) if workspace is not None else None
    within_workspace = workspace_path is not None and is_relative_to_path(
        normalized, workspace_path
    )
    if _is_blocked_path(normalized, workspace_path):
        return PathRiskClassification(
            normalized_path=normalized_text,
            within_workspace=within_workspace,
            protected=True,
            reason="sensitive_path",
        )
    if _is_system_write_path(normalized):
        return PathRiskClassification(
            normalized_path=normalized_text,
            within_workspace=within_workspace,
            protected=True,
            reason="system_path",
        )
    if not within_workspace and _has_protected_metadata_part(normalized):
        return PathRiskClassification(
            normalized_path=normalized_text,
            within_workspace=False,
            protected=True,
            reason="protected_metadata",
        )
    return PathRiskClassification(
        normalized_path=normalized_text,
        within_workspace=within_workspace,
        low_risk_user_area=_is_low_risk_user_area(normalized),
    )


def trusted_write_auto_grant_allowed(
    path: str | os.PathLike[str],
    *,
    workspace: str | os.PathLike[str] | None,
) -> bool:
    classification = classify_path_for_sandbox(path, workspace=workspace)
    return not classification.protected and (
        classification.within_workspace or classification.low_risk_user_area
    )


def _iter_mount_roots(mounts: Iterable[Any]) -> Iterable[tuple[DecisionPath, MountAccess]]:
    for item in mounts:
        raw_path: Any
        raw_access: Any
        if isinstance(item, Mapping):
            raw_path = item.get("path") or item.get("host_path")
            raw_access = item.get("access") or item.get("mode")
        else:
            raw_path = getattr(item, "path", None) or getattr(item, "host_path", None)
            raw_access = getattr(item, "access", None) or getattr(item, "mode", None)
        if not isinstance(raw_path, (str, os.PathLike)):
            continue
        try:
            root = _normalize_decision_path(raw_path)
        except (OSError, RuntimeError):
            continue
        yield root, normalize_mount_access(raw_access)


def _is_blocked_path(path: PurePath, workspace: PurePath | None) -> bool:
    if _is_filesystem_root(path):
        return True
    marker = sensitive_path_marker(
        str(path),
        workspace=str(workspace) if workspace is not None else None,
    )
    if marker is not None:
        return True
    normalized = str(path).replace("\\", "/")
    for prefix in _POSIX_BLOCKED_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix.rstrip("/") + "/"):
            return True
    return _is_windows_sensitive_path(str(path))


def _is_system_write_path(path: PurePath) -> bool:
    if os.name == "nt":
        return False
    normalized = str(path).replace("\\", "/")
    for prefix in _POSIX_SYSTEM_WRITE_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def _has_protected_metadata_part(path: PurePath) -> bool:
    for part in path.parts:
        lower = part.lower()
        if lower in _PROTECTED_METADATA_PARTS:
            return True
        if lower.startswith(".") and lower not in {".", ".."}:
            return True
    return False


def _is_low_risk_user_area(path: PurePath) -> bool:
    roots = _low_risk_user_roots()
    return any(is_relative_to_path(path, root) for root in roots)


def _low_risk_user_roots() -> tuple[DecisionPath, ...]:
    roots: list[DecisionPath] = []
    for raw in (
        os.environ.get("TMPDIR"),
        os.environ.get("TEMP"),
        os.environ.get("TMP"),
        tempfile.gettempdir(),
        "/tmp",
        "/private/tmp",
        "/var/tmp",
        str(Path.home()),
    ):
        if not raw:
            continue
        try:
            root = _normalize_decision_path(raw)
        except (OSError, RuntimeError, ValueError):
            continue
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _is_filesystem_root(path: PurePath) -> bool:
    try:
        if not path.anchor:
            return False
        return path == type(path)(path.anchor)
    except (OSError, RuntimeError, ValueError):
        return False


def _is_windows_sensitive_path(raw_path: str) -> bool:
    from opensquilla.sandbox.backend.windows_default_roots import windows_sensitive_marker

    return windows_sensitive_marker(raw_path) is not None


__all__ = [
    "MountAccess",
    "MountDecision",
    "MountStatus",
    "PathRiskClassification",
    "classify_path_for_sandbox",
    "decide_path_access",
    "is_relative_to_path",
    "normalize_mount_access",
    "normalize_path",
    "trusted_write_auto_grant_allowed",
]
