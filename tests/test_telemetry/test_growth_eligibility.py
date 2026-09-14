from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.telemetry.consent import (
    CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
    CURRENT_RELIABILITY_NOTICE_VERSION,
    TelemetryScope,
    resolve_scope_consent,
)
from opensquilla.telemetry.growth.eligibility import (
    GrowthEligibilityState,
    GrowthProfileEvidence,
    activate_growth_cohort,
    classify_initial_growth_eligibility,
    close_growth_cohort_without_observation,
    growth_milestone_collection_allowed,
    revoke_growth_cohort,
)

VALID_CONSENT_TIME = "2026-09-01T08:00:00Z"


def _consent(
    *,
    enabled: bool | None = True,
    global_disabled: bool = False,
    transient_forced_off: bool = False,
    scope: TelemetryScope = TelemetryScope.GROWTH,
):
    privacy = SimpleNamespace(
        disable_network_observability=global_disabled,
        product_analytics_enabled=enabled,
        product_analytics_notice_version=CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
        product_analytics_consented_at_utc=VALID_CONSENT_TIME,
        reliability_diagnostics_enabled=True,
        reliability_notice_version=CURRENT_RELIABILITY_NOTICE_VERSION,
        reliability_consented_at_utc=VALID_CONSENT_TIME,
    )
    return resolve_scope_consent(
        scope,
        config=SimpleNamespace(privacy=privacy),
        env={},
        transient_forced_off=transient_forced_off,
    )


def test_only_explicitly_proven_clean_profile_is_a_new_candidate() -> None:
    assert (
        classify_initial_growth_eligibility(GrowthProfileEvidence(fresh_profile_proof=True))
        is GrowthEligibilityState.NEW_CANDIDATE
    )
    assert (
        classify_initial_growth_eligibility(GrowthProfileEvidence())
        is GrowthEligibilityState.PREEXISTING
    )


@pytest.mark.parametrize(
    "evidence_field",
    [
        "has_config",
        "has_credentials",
        "has_session_data",
        "has_legacy_telemetry_state",
        "has_other_profile_state",
        "imported_or_migrated_profile",
    ],
)
def test_any_preexisting_evidence_prevents_upgrade_backfill(
    evidence_field: str,
) -> None:
    evidence = GrowthProfileEvidence(
        fresh_profile_proof=True,
        **{evidence_field: True},
    )

    assert classify_initial_growth_eligibility(evidence) is GrowthEligibilityState.PREEXISTING


@pytest.mark.parametrize("persisted_state", list(GrowthEligibilityState))
def test_valid_persisted_state_is_stable(
    persisted_state: GrowthEligibilityState,
) -> None:
    result = classify_initial_growth_eligibility(
        GrowthProfileEvidence(fresh_profile_proof=True),
        persisted_state=persisted_state,
    )

    assert result is persisted_state


def test_unknown_persisted_state_fails_closed_as_preexisting() -> None:
    result = classify_initial_growth_eligibility(
        GrowthProfileEvidence(fresh_profile_proof=True),
        persisted_state="future-or-corrupt",
    )

    assert result is GrowthEligibilityState.PREEXISTING


def test_activation_requires_effective_growth_consent() -> None:
    assert (
        activate_growth_cohort(GrowthEligibilityState.NEW_CANDIDATE, _consent())
        is GrowthEligibilityState.ACTIVE
    )
    assert (
        activate_growth_cohort(
            GrowthEligibilityState.NEW_CANDIDATE,
            _consent(enabled=None),
        )
        is GrowthEligibilityState.NEW_CANDIDATE
    )
    assert (
        activate_growth_cohort(
            GrowthEligibilityState.NEW_CANDIDATE,
            _consent(global_disabled=True),
        )
        is GrowthEligibilityState.NEW_CANDIDATE
    )
    assert (
        activate_growth_cohort(
            GrowthEligibilityState.NEW_CANDIDATE,
            _consent(transient_forced_off=True),
        )
        is GrowthEligibilityState.NEW_CANDIDATE
    )


def test_activation_rejects_reliability_consent() -> None:
    with pytest.raises(ValueError, match="Growth consent"):
        activate_growth_cohort(
            GrowthEligibilityState.NEW_CANDIDATE,
            _consent(scope=TelemetryScope.RELIABILITY),
        )


def test_milestone_collection_requires_active_state_and_live_consent() -> None:
    consent = _consent()

    assert growth_milestone_collection_allowed(GrowthEligibilityState.ACTIVE, consent)
    assert not growth_milestone_collection_allowed(
        GrowthEligibilityState.NEW_CANDIDATE,
        consent,
    )
    assert not growth_milestone_collection_allowed(
        GrowthEligibilityState.ACTIVE,
        _consent(enabled=False),
    )
    assert not growth_milestone_collection_allowed(
        GrowthEligibilityState.ACTIVE,
        _consent(transient_forced_off=True),
    )


@pytest.mark.parametrize(
    "initial_state",
    [GrowthEligibilityState.NEW_CANDIDATE, GrowthEligibilityState.ACTIVE],
)
def test_missed_first_observation_closes_cohort_irreversibly(
    initial_state: GrowthEligibilityState,
) -> None:
    closed = close_growth_cohort_without_observation(initial_state)

    assert closed is GrowthEligibilityState.CLOSED_UNOBSERVED
    assert activate_growth_cohort(closed, _consent()) is closed
    assert not growth_milestone_collection_allowed(closed, _consent())


@pytest.mark.parametrize("initial_state", list(GrowthEligibilityState))
def test_revocation_is_irreversible(initial_state: GrowthEligibilityState) -> None:
    revoked = revoke_growth_cohort(initial_state)

    assert revoked is GrowthEligibilityState.REVOKED
    assert activate_growth_cohort(revoked, _consent()) is revoked
    assert not growth_milestone_collection_allowed(revoked, _consent())
