"""Resolve requested Safe/Full modes against principal and host capability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opensquilla.sandbox.capability_service import CapabilityReport
from opensquilla.sandbox.run_mode import RunMode, normalize_run_mode


class ModeResolutionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class ResolvedMode:
    desired_mode: RunMode
    effective_mode: RunMode
    fallback_reason: str | None = None
    confirmation_required: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "desiredMode": self.desired_mode.value,
            "effectiveMode": self.effective_mode.value,
            "fallbackReason": self.fallback_reason,
            "confirmationRequired": self.confirmation_required,
        }


def _has_capability(principal: Any, capability: str) -> bool:
    has = getattr(principal, "has", None)
    if callable(has):
        return bool(has(capability))
    return capability in getattr(principal, "capabilities", ())


def resolve_mode(
    desired_mode: RunMode | str,
    principal: Any,
    capability: CapabilityReport,
) -> ResolvedMode:
    desired = normalize_run_mode(desired_mode)
    host_execute = _has_capability(principal, "host.execute")
    if desired is RunMode.FULL:
        if not host_execute:
            raise ModeResolutionError("host_capability_required")
        return ResolvedMode(desired_mode=desired, effective_mode=RunMode.FULL)

    if capability.available:
        return ResolvedMode(desired_mode=desired, effective_mode=RunMode.SAFE)
    if host_execute:
        return ResolvedMode(
            desired_mode=desired,
            effective_mode=RunMode.FULL,
            fallback_reason=capability.code,
            confirmation_required=True,
        )
    raise ModeResolutionError("sandbox_unavailable_for_guest")


__all__ = [
    "ModeResolutionError",
    "ResolvedMode",
    "resolve_mode",
]
