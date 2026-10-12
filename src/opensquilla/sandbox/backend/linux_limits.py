"""Linux process resource-limit helpers."""

from __future__ import annotations

import sys
from collections.abc import Callable

from opensquilla.sandbox.types import ResourceLimits

try:
    import resource
except ImportError:
    resource = None  # type: ignore[assignment]


def resource_preexec_from_limits(limits: ResourceLimits) -> Callable[[], None] | None:
    return resource_preexec_from_policy(
        {
            "cpuSeconds": limits.cpu_seconds,
            "memoryMb": limits.memory_mb,
            "pids": limits.pids,
        }
    )


def resource_preexec_from_policy(policy: dict[str, object]) -> Callable[[], None] | None:
    if not sys.platform.startswith("linux") or resource is None:
        return None

    limits: list[tuple[int, int]] = []
    cpu_seconds = _positive_int(policy.get("cpuSeconds"))
    if cpu_seconds is not None:
        limits.append((resource.RLIMIT_CPU, cpu_seconds))
    pids = _positive_int(policy.get("pids"))
    if pids is not None and hasattr(resource, "RLIMIT_NPROC"):
        limits.append((resource.RLIMIT_NPROC, pids))
    if not limits:
        return None

    def apply_limits() -> None:
        for resource_id, value in limits:
            _set_soft_limit(resource_id, value)

    return apply_limits


def _positive_int(value: object) -> int | None:
    if not isinstance(value, (str, bytes, int, float)):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _set_soft_limit(resource_id: int, value: int) -> None:
    if resource is None:
        return

    try:
        _, hard = resource.getrlimit(resource_id)
        soft = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(resource_id, (soft, hard))
    except (OSError, ValueError):
        return


__all__ = ["resource_preexec_from_limits", "resource_preexec_from_policy"]
