"""Deterministic routing facts for Artifact IDE turns.

The classifier still chooses the baseline tier.  These bounded facts only set
the minimum tier required by an already-validated artifact interaction.  They
are deliberately content-free so router telemetry never needs document names,
identifiers, source text, DOM, anchors, or selection contents.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from opensquilla.router_tiers import normalize_text_tier, tier_index


class ArtifactRoutingUnavailableError(RuntimeError):
    """No configured text tier can satisfy an Artifact IDE capability floor."""

    code = "artifact_router_tier_unavailable"

    def __init__(self, facts: ArtifactRoutingFacts, valid_tiers: list[str]) -> None:
        configured = ", ".join(valid_tiers) if valid_tiers else "none"
        super().__init__(
            "No configured SquillaRouter tier satisfies the "
            f"{facts.minimum_tier} minimum for {facts.operation_class.value} "
            f"(configured text tiers: {configured})."
        )
        self.minimum_tier = facts.minimum_tier
        self.operation_class = facts.operation_class.value


class ArtifactFormat(StrEnum):
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    HTML = "html"


class ArtifactOperationClass(StrEnum):
    OPEN = "open"
    COMMENT = "comment"
    SELECTION_EDIT = "selection_edit"
    STRUCTURAL_EDIT = "structural_edit"
    BROWSER_USE = "browser_use"
    CONFLICT_RECOVERY = "conflict_recovery"


_MINIMUM_TIER: dict[ArtifactOperationClass, str] = {
    ArtifactOperationClass.OPEN: "c1",
    ArtifactOperationClass.COMMENT: "c2",
    ArtifactOperationClass.SELECTION_EDIT: "c2",
    ArtifactOperationClass.STRUCTURAL_EDIT: "c3",
    ArtifactOperationClass.BROWSER_USE: "c3",
    ArtifactOperationClass.CONFLICT_RECOVERY: "c3",
}


@dataclass(frozen=True, slots=True)
class ArtifactRoutingFacts:
    artifact_format: ArtifactFormat
    operation_class: ArtifactOperationClass = ArtifactOperationClass.OPEN

    @property
    def minimum_tier(self) -> str:
        return _MINIMUM_TIER[self.operation_class]

    def to_telemetry(self) -> dict[str, str]:
        """Return the only Artifact IDE values permitted in router telemetry."""

        return {
            "artifact_format": self.artifact_format.value,
            "artifact_operation_class": self.operation_class.value,
            "artifact_minimum_tier": self.minimum_tier,
        }

    @classmethod
    def from_values(
        cls,
        artifact_format: object,
        operation_class: object = ArtifactOperationClass.OPEN,
    ) -> ArtifactRoutingFacts:
        return cls(
            artifact_format=ArtifactFormat(str(artifact_format).strip().lower()),
            operation_class=ArtifactOperationClass(
                str(operation_class).strip().lower()
            ),
        )


def effective_artifact_floor(
    facts: ArtifactRoutingFacts | None,
    valid_tiers: list[str],
) -> str | None:
    """Resolve the configured tier at or above the required canonical floor."""

    if facts is None:
        return None
    minimum = normalize_text_tier(facts.minimum_tier)
    if minimum is None:
        return None
    candidates = [
        normalize_text_tier(tier) or tier
        for tier in valid_tiers
        if tier_index(normalize_text_tier(tier) or tier) >= tier_index(minimum)
    ]
    candidates = [tier for tier in candidates if tier_index(tier) >= 0]
    return min(candidates, key=tier_index) if candidates else None


__all__ = [
    "ArtifactFormat",
    "ArtifactOperationClass",
    "ArtifactRoutingUnavailableError",
    "ArtifactRoutingFacts",
    "effective_artifact_floor",
]
