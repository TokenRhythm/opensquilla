"""Wire-safe lifecycle and diagnostic contracts for Community skills.

These types deliberately stay independent from the loader, installer, and RPC
layers.  They describe observations that those layers can publish later without
turning volatile catalog or environment state into lockfile state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DiagnosticSeverity(StrEnum):
    """Severity of one machine-readable Skill diagnostic."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DiagnosticPhase(StrEnum):
    """Stable pipeline phase associated with a Skill diagnostic."""

    SOURCE = "source"
    FETCH = "fetch"
    ARCHIVE = "archive"
    MANIFEST = "manifest"
    COMPATIBILITY = "compatibility"
    SECURITY = "security"
    STORE = "store"
    LOCK = "lock"
    CATALOG = "catalog"
    READINESS = "readiness"
    INVOCATION = "invocation"


@dataclass(frozen=True)
class SkillDiagnostic:
    """One stable, serializable explanation of a Skill lifecycle observation."""

    code: str
    severity: DiagnosticSeverity
    phase: DiagnosticPhase
    message: str
    blocking: bool = False
    path: str = ""
    field_name: str = ""
    hint: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the additive wire shape shared by future CLI/RPC surfaces."""

        return {
            "code": self.code,
            "severity": self.severity.value,
            "phase": self.phase.value,
            "message": self.message,
            "blocking": self.blocking,
            "path": self.path,
            "field_name": self.field_name,
            "hint": self.hint,
            "details": dict(self.details),
        }

    def as_dict(self) -> dict[str, Any]:
        """Compatibility alias for dataclass-oriented callers."""

        return self.to_dict()


class SkillInstallState(StrEnum):
    TRACKED = "tracked"
    UNTRACKED = "untracked"
    MISSING = "missing"
    DRIFTED = "drifted"


class SkillLoadState(StrEnum):
    LOADED = "loaded"
    VALIDATED_OFFLINE = "validated_offline"
    REJECTED = "rejected"
    NOT_DISCOVERED = "not_discovered"
    SERVING_PREVIOUS = "serving_previous"


class SkillSelectionState(StrEnum):
    ACTIVE = "active"
    SHADOWED = "shadowed"
    DISABLED = "disabled"
    HIDDEN = "hidden"


class SkillCompatibilityState(StrEnum):
    NATIVE = "native"
    INSTRUCTION_ONLY = "instruction_only"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"


class SkillReadinessState(StrEnum):
    READY = "ready"
    NEEDS_SETUP = "needs_setup"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SkillInvocationCapabilities:
    """Invocation modes actually supported for one loaded Skill instance."""

    model_catalog: bool = False
    skill_view: bool = False
    user_completion: bool = False
    direct_command: bool = False
    argument_substitution: bool = False
    scoped_tool_permissions: bool = False
    sandbox_execution: str = "unknown"

    def to_dict(self) -> dict[str, bool | str]:
        return {
            "model_catalog": self.model_catalog,
            "skill_view": self.skill_view,
            "user_completion": self.user_completion,
            "direct_command": self.direct_command,
            "argument_substitution": self.argument_substitution,
            "scoped_tool_permissions": self.scoped_tool_permissions,
            "sandbox_execution": self.sandbox_execution,
        }

    def as_dict(self) -> dict[str, bool | str]:
        return self.to_dict()


@dataclass(frozen=True)
class SkillLifecycle:
    """Orthogonal lifecycle axes for one Skill instance.

    ``usable`` is derived rather than persisted.  ``None`` means the structural
    and invocation gates pass but runtime readiness has not been verified.
    """

    install_state: SkillInstallState
    load_state: SkillLoadState
    selection_state: SkillSelectionState
    compatibility_state: SkillCompatibilityState
    readiness_state: SkillReadinessState
    invocation: SkillInvocationCapabilities = field(default_factory=SkillInvocationCapabilities)

    @property
    def usable(self) -> bool | None:
        if self.install_state is SkillInstallState.MISSING:
            return False
        if self.load_state not in {
            SkillLoadState.LOADED,
            SkillLoadState.SERVING_PREVIOUS,
        }:
            return False
        if self.selection_state in {
            SkillSelectionState.SHADOWED,
            SkillSelectionState.DISABLED,
        }:
            return False
        if self.compatibility_state is SkillCompatibilityState.UNSUPPORTED:
            return False
        if not (
            self.invocation.model_catalog
            or self.invocation.skill_view
            or self.invocation.user_completion
            or self.invocation.direct_command
        ):
            return False
        if self.readiness_state is SkillReadinessState.NEEDS_SETUP:
            return False
        if self.readiness_state is SkillReadinessState.UNKNOWN:
            return None
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "install_state": self.install_state.value,
            "load_state": self.load_state.value,
            "selection_state": self.selection_state.value,
            "compatibility_state": self.compatibility_state.value,
            "readiness_state": self.readiness_state.value,
            "invocation": self.invocation.to_dict(),
            "usable": self.usable,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.to_dict()


__all__ = [
    "DiagnosticPhase",
    "DiagnosticSeverity",
    "SkillCompatibilityState",
    "SkillDiagnostic",
    "SkillInstallState",
    "SkillInvocationCapabilities",
    "SkillLifecycle",
    "SkillLoadState",
    "SkillReadinessState",
    "SkillSelectionState",
]
