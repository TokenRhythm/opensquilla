from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

HealthSeverity = Literal["error", "warn", "info", "ok"]
HealthStatus = Literal["ready", "degraded", "action_required", "unavailable"]
ReadinessImpact = Literal["blocks_ready", "degrades", "optional", "none"]

_COUNT_KEYS: tuple[HealthSeverity, ...] = ("error", "warn", "info", "ok")
_IMPACT_KEYS: tuple[ReadinessImpact, ...] = (
    "blocks_ready",
    "degrades",
    "optional",
    "none",
)
_FINDING_PRIORITY: dict[HealthSeverity, int] = {
    "error": 0,
    "warn": 1,
    "info": 2,
    "ok": 3,
}
_IMPACT_PRIORITY: dict[ReadinessImpact, int] = {
    "blocks_ready": 0,
    "degrades": 1,
    "optional": 2,
    "none": 3,
}
_DEFAULT_IMPACT_BY_SEVERITY: dict[HealthSeverity, ReadinessImpact] = {
    "error": "blocks_ready",
    "warn": "degrades",
    "info": "optional",
    "ok": "none",
}


@dataclass(frozen=True)
class FixStep:
    label: str
    command: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"label": self.label}
        if self.command:
            payload["command"] = self.command
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class HealthFinding:
    id: str
    severity: HealthSeverity
    surface: str
    title: str
    detail: str
    readiness_impact: ReadinessImpact | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    fix_steps: list[FixStep] = field(default_factory=list)
    # True when applying the recommended recovery changes requires a gateway restart
    # before the running process observes them.
    restart_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "surface": self.surface,
            "title": self.title,
            "detail": self.detail,
            "readinessImpact": _readiness_impact(self),
            "evidence": self.evidence,
            "fixSteps": [step.to_dict() for step in self.fix_steps],
            "restartRequired": self.restart_required,
        }


def _readiness_impact(finding: HealthFinding) -> ReadinessImpact:
    return finding.readiness_impact or _DEFAULT_IMPACT_BY_SEVERITY[finding.severity]


def summarize_impact_counts(impact_counts: Mapping[str, int], *, attention_count: int = 0) -> str:
    """Human summary line for a readiness report.

    Shared with the CLI's local readiness refresh so the operator-facing
    wording cannot drift between the gateway report and ``opensquilla doctor``.
    ``attention_count`` is the number of warn-severity optional-impact
    findings — operator action items, labeled distinctly from setup items.
    """
    parts: list[str] = []
    if impact_counts["blocks_ready"]:
        label = "action" if impact_counts["blocks_ready"] == 1 else "actions"
        parts.append(f"{impact_counts['blocks_ready']} {label} required")
    if impact_counts["degrades"]:
        label = "check" if impact_counts["degrades"] == 1 else "checks"
        if not impact_counts["blocks_ready"]:
            parts.append(f"Ready, {impact_counts['degrades']} degraded {label}")
        else:
            parts.append(f"{impact_counts['degrades']} degraded {label}")
    if parts:
        return ", ".join(parts)
    if impact_counts["optional"]:
        # An optional-impact warn is an operator action item (senders waiting
        # on approval, sends needing confirmation) — calling it a "setup item"
        # would mislabel work someone is actively blocked on.
        setup_count = impact_counts["optional"] - attention_count
        segments: list[str] = []
        if attention_count:
            label = "item" if attention_count == 1 else "items"
            segments.append(f"{attention_count} {label} awaiting operator action")
        if setup_count:
            label = "item" if setup_count == 1 else "items"
            segments.append(f"{setup_count} optional setup {label}")
        return "Ready, " + ", ".join(segments)
    return "Ready"


def _status(impact_counts: Mapping[str, int]) -> HealthStatus:
    # "unavailable" is reserved for callers that cannot reach doctor.status at all.
    if impact_counts["blocks_ready"]:
        return "action_required"
    if impact_counts["degrades"]:
        return "degraded"
    return "ready"


def build_report(findings: list[HealthFinding]) -> dict[str, Any]:
    counts: dict[HealthSeverity, int] = {key: 0 for key in _COUNT_KEYS}
    # str-keyed so the shared summary helper (Mapping[str, int]) accepts it;
    # writes still come only from _IMPACT_KEYS.
    impact_counts: dict[str, int] = {key: 0 for key in _IMPACT_KEYS}
    attention_count = 0
    for finding in findings:
        counts[finding.severity] += 1
        impact_counts[_readiness_impact(finding)] += 1
        if finding.severity == "warn" and _readiness_impact(finding) == "optional":
            attention_count += 1
    status = _status(impact_counts)
    ordered_findings = sorted(
        enumerate(findings),
        key=lambda item: (
            _IMPACT_PRIORITY[_readiness_impact(item[1])],
            _FINDING_PRIORITY[item[1].severity],
            item[0],
        ),
    )
    return {
        "status": status,
        "ready": impact_counts["blocks_ready"] == 0,
        "summary": summarize_impact_counts(impact_counts, attention_count=attention_count),
        "counts": counts,
        "impactCounts": impact_counts,
        "findings": [finding.to_dict() for _, finding in ordered_findings],
    }
