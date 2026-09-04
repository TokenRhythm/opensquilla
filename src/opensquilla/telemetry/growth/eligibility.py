"""Fail-closed new-user cohort eligibility for growth milestones."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from opensquilla.telemetry.consent import ScopeConsentState, TelemetryScope


class GrowthEligibilityState(StrEnum):
    NEW_CANDIDATE = "new_candidate"
    PREEXISTING = "preexisting"
    ACTIVE = "active"
    CLOSED_UNOBSERVED = "closed_unobserved"
    REVOKED = "revoked"


@dataclass(frozen=True)
class GrowthProfileEvidence:
    """Content-free evidence captured before a new build writes profile state.

    ``fresh_profile_proof`` must come from the profile/installer lifecycle, not
    from the mere absence of an old telemetry file.  This avoids treating an
    upgrade from a telemetry-disabled release as a new acquisition cohort.
    """

    fresh_profile_proof: bool = False
    has_config: bool = False
    has_credentials: bool = False
    has_session_data: bool = False
    has_legacy_telemetry_state: bool = False
    has_other_profile_state: bool = False
    imported_or_migrated_profile: bool = False

    @property
    def has_preexisting_evidence(self) -> bool:
        return any(
            (
                self.has_config,
                self.has_credentials,
                self.has_session_data,
                self.has_legacy_telemetry_state,
                self.has_other_profile_state,
                self.imported_or_migrated_profile,
            )
        )


def classify_initial_growth_eligibility(
    evidence: GrowthProfileEvidence,
    *,
    persisted_state: GrowthEligibilityState | str | None = None,
) -> GrowthEligibilityState:
    """Classify a profile without ever backfilling an ambiguous upgrade.

    A valid persisted decision wins.  Unknown persisted state, missing fresh
    proof, imports, and any existing profile artifact all fail closed as
    ``PREEXISTING``.
    """

    if persisted_state is not None:
        try:
            return GrowthEligibilityState(persisted_state)
        except ValueError:
            return GrowthEligibilityState.PREEXISTING
    if evidence.fresh_profile_proof and not evidence.has_preexisting_evidence:
        return GrowthEligibilityState.NEW_CANDIDATE
    return GrowthEligibilityState.PREEXISTING


def activate_growth_cohort(
    state: GrowthEligibilityState | str,
    consent: ScopeConsentState,
) -> GrowthEligibilityState:
    """Activate only a proven-new profile with effective Growth consent."""

    normalized = GrowthEligibilityState(state)
    if consent.scope is not TelemetryScope.GROWTH:
        raise ValueError("growth eligibility requires Growth consent state")
    if normalized is GrowthEligibilityState.NEW_CANDIDATE and consent.enabled:
        return GrowthEligibilityState.ACTIVE
    return normalized


def close_growth_cohort_without_observation(
    state: GrowthEligibilityState | str,
) -> GrowthEligibilityState:
    """Permanently prevent later consent from backfilling a missed first step."""

    normalized = GrowthEligibilityState(state)
    if normalized in {
        GrowthEligibilityState.NEW_CANDIDATE,
        GrowthEligibilityState.ACTIVE,
    }:
        return GrowthEligibilityState.CLOSED_UNOBSERVED
    return normalized


def revoke_growth_cohort(
    state: GrowthEligibilityState | str,
) -> GrowthEligibilityState:
    """Make revocation irreversible for the original new-user journey."""

    GrowthEligibilityState(state)
    return GrowthEligibilityState.REVOKED


def growth_milestone_collection_allowed(
    state: GrowthEligibilityState | str,
    consent: ScopeConsentState,
) -> bool:
    """Return whether a first-milestone fact may be recorded right now."""

    if consent.scope is not TelemetryScope.GROWTH:
        return False
    return GrowthEligibilityState(state) is GrowthEligibilityState.ACTIVE and consent.enabled
