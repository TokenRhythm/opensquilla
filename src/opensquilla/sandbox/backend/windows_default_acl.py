"""ACL refresh planning for the Windows default sandbox."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from opensquilla.sandbox.run_mode import RunMode, normalize_run_mode


class AclAccess(StrEnum):
    RX = "RX"
    RWX = "RWX"


class AclGrantKind(StrEnum):
    REQUIRED = "required"
    POLICY = "policy"
    EXPANSION = "expansion"


@dataclass(frozen=True)
class AclGrant:
    path: Path
    access: AclAccess
    kind: AclGrantKind


@dataclass(frozen=True)
class AclDeniedGrant:
    grant: AclGrant
    reason: str


@dataclass(frozen=True)
class AclRefreshPlan:
    auto_grants: tuple[AclGrant, ...]
    approval_required: tuple[AclGrant, ...]
    denied: tuple[AclDeniedGrant, ...]


class AclRefreshDecisionKind(StrEnum):
    AUTO = "auto"
    ASK = "ask"
    DENY = "deny"
    HOST = "host"


def plan_acl_refresh(
    *,
    run_mode: RunMode | str,
    required: Iterable[AclGrant],
    policy: Iterable[AclGrant],
    expansion: Iterable[AclGrant],
    sensitive_marker: Callable[[Path], str | None],
    required_policy_sensitive_marker: Callable[[Path], str | None] | None = None,
) -> AclRefreshPlan:
    mode = normalize_run_mode(run_mode)
    if mode is RunMode.FULL:
        return AclRefreshPlan(auto_grants=(), approval_required=(), denied=())

    base_marker = required_policy_sensitive_marker or sensitive_marker
    auto: list[AclGrant] = []
    ask: list[AclGrant] = []
    denied: list[AclDeniedGrant] = []

    for grant in _dedupe_grants((*required, *policy)):
        marker = base_marker(grant.path)
        if marker:
            denied.append(AclDeniedGrant(grant=grant, reason=marker))
        else:
            auto.append(grant)

    for grant in _dedupe_grants(tuple(expansion)):
        marker = sensitive_marker(grant.path)
        if marker:
            denied.append(AclDeniedGrant(grant=grant, reason=marker))
        elif mode is RunMode.TRUSTED:
            auto.append(grant)
        else:
            ask.append(grant)

    return AclRefreshPlan(
        auto_grants=tuple(auto),
        approval_required=tuple(ask),
        denied=tuple(denied),
    )


def _dedupe_grants(grants: Iterable[AclGrant]) -> tuple[AclGrant, ...]:
    seen: set[tuple[str, str, str]] = set()
    result: list[AclGrant] = []
    for grant in grants:
        key = (str(grant.path).lower(), grant.access.value, grant.kind.value)
        if key in seen:
            continue
        seen.add(key)
        result.append(grant)
    return tuple(result)


__all__ = [
    "AclAccess",
    "AclDeniedGrant",
    "AclGrant",
    "AclGrantKind",
    "AclRefreshDecisionKind",
    "AclRefreshPlan",
    "plan_acl_refresh",
]
