from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway.config import GatewayConfig, PrivacyConfig
from opensquilla.observability.network_policy import telemetry_scope_forced_off_reasons
from opensquilla.telemetry.consent import (
    CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
    CURRENT_RELIABILITY_NOTICE_VERSION,
    ConsentCheckpoint,
    ConsentDecision,
    LocalStateDirective,
    TelemetryScope,
    resolve_scope_consent,
    scope_collection_enabled,
    scope_enqueue_enabled,
    scope_send_enabled,
)

VALID_NOTICE = CURRENT_RELIABILITY_NOTICE_VERSION
VALID_CONSENT_TIME = "2026-09-01T08:00:00Z"


def _config(**privacy: object) -> SimpleNamespace:
    return SimpleNamespace(privacy=SimpleNamespace(**privacy))


def test_privacy_config_defaults_leave_both_scopes_unset_and_unpersisted() -> None:
    privacy = PrivacyConfig()

    assert privacy.reliability_diagnostics_enabled is None
    assert privacy.product_analytics_enabled is None
    persisted = GatewayConfig(privacy=privacy).to_toml_dict()["privacy"]
    assert "reliability_diagnostics_enabled" not in persisted
    assert "product_analytics_enabled" not in persisted


def test_legacy_and_incomplete_privacy_configurations_still_load() -> None:
    legacy = GatewayConfig.model_validate({"privacy": {"disable_network_observability": True}})
    incomplete = GatewayConfig.model_validate(
        {
            "privacy": {
                "reliability_diagnostics_enabled": True,
                "product_analytics_enabled": False,
                "product_analytics_notice_version": "older-notice",
            }
        }
    )

    assert legacy.privacy.disable_network_observability is True
    assert legacy.privacy.reliability_diagnostics_enabled is None
    assert legacy.privacy.product_analytics_enabled is None
    assert incomplete.privacy.reliability_diagnostics_enabled is True
    assert incomplete.privacy.reliability_notice_version is None
    assert incomplete.privacy.product_analytics_enabled is False
    assert not resolve_scope_consent(
        TelemetryScope.RELIABILITY,
        config=incomplete,
        env={},
    ).enabled


@pytest.mark.parametrize("scope", list(TelemetryScope))
def test_unset_and_explicit_false_are_both_off_but_remain_distinct(
    scope: TelemetryScope,
) -> None:
    unset = resolve_scope_consent(scope, config=_config(), env={})
    field = (
        "reliability_diagnostics_enabled"
        if scope is TelemetryScope.RELIABILITY
        else "product_analytics_enabled"
    )
    declined = resolve_scope_consent(scope, config=_config(**{field: False}), env={})

    assert unset.enabled is False
    assert unset.decision is ConsentDecision.UNSET
    assert unset.persistently_disabled is False
    assert declined.enabled is False
    assert declined.decision is ConsentDecision.DECLINED
    assert declined.persistently_disabled is True


def test_explicit_decline_is_persisted_while_unset_metadata_is_omitted() -> None:
    persisted = GatewayConfig(
        privacy=PrivacyConfig(product_analytics_enabled=False)
    ).to_toml_dict()["privacy"]

    assert persisted["product_analytics_enabled"] is False
    assert "product_analytics_notice_version" not in persisted
    assert "product_analytics_consented_at_utc" not in persisted


@pytest.mark.parametrize(
    ("privacy", "expected_reason"),
    [
        (
            {"reliability_diagnostics_enabled": True},
            "consent:incomplete",
        ),
        (
            {
                "reliability_diagnostics_enabled": True,
                "reliability_notice_version": VALID_NOTICE,
            },
            "consent:incomplete",
        ),
        (
            {
                "reliability_diagnostics_enabled": True,
                "reliability_notice_version": VALID_NOTICE,
                "reliability_consented_at_utc": "not-a-time",
            },
            "consent:incomplete",
        ),
        (
            {
                "reliability_diagnostics_enabled": True,
                "reliability_notice_version": VALID_NOTICE,
                "reliability_consented_at_utc": "2026-09-01T08:00:00+08:00",
            },
            "consent:incomplete",
        ),
    ],
)
def test_grant_fails_closed_without_complete_utc_consent_record(
    privacy: dict[str, object],
    expected_reason: str,
) -> None:
    state = resolve_scope_consent(
        TelemetryScope.RELIABILITY,
        config=_config(**privacy),
        env={},
    )

    assert state.decision is ConsentDecision.GRANTED
    assert state.enabled is False
    assert expected_reason in state.block_reasons


def test_complete_consent_enables_only_its_own_scope() -> None:
    config = _config(
        reliability_diagnostics_enabled=True,
        reliability_notice_version=VALID_NOTICE,
        reliability_consented_at_utc=VALID_CONSENT_TIME,
    )

    assert scope_collection_enabled(TelemetryScope.RELIABILITY, config=config, env={})
    assert not scope_collection_enabled(TelemetryScope.GROWTH, config=config, env={})


def test_stale_notice_fails_closed_without_erasing_granted_decision() -> None:
    state = resolve_scope_consent(
        TelemetryScope.GROWTH,
        config=_config(
            product_analytics_enabled=True,
            product_analytics_notice_version="2026-08-01",
            product_analytics_consented_at_utc=VALID_CONSENT_TIME,
        ),
        env={},
    )

    assert state.decision is ConsentDecision.GRANTED
    assert state.record_complete is True
    assert state.notice_current is False
    assert state.enabled is False
    assert state.block_reasons == ("consent:notice_stale",)


def test_notice_requirement_can_be_explicitly_overridden() -> None:
    preview_notice = "test-preview-notice"
    state = resolve_scope_consent(
        TelemetryScope.GROWTH,
        config=_config(
            product_analytics_enabled=True,
            product_analytics_notice_version=preview_notice,
            product_analytics_consented_at_utc=VALID_CONSENT_TIME,
        ),
        env={},
        required_notice_version=preview_notice,
    )

    assert state.enabled is True


def test_legacy_global_false_is_not_scoped_consent_and_true_is_total_veto() -> None:
    legacy_false = resolve_scope_consent(
        TelemetryScope.GROWTH,
        config=_config(disable_network_observability=False),
        env={},
    )
    granted_but_vetoed = resolve_scope_consent(
        TelemetryScope.GROWTH,
        config=_config(
            disable_network_observability=True,
            product_analytics_enabled=True,
            product_analytics_notice_version=VALID_NOTICE,
            product_analytics_consented_at_utc=VALID_CONSENT_TIME,
        ),
        env={},
    )

    assert legacy_false.decision is ConsentDecision.UNSET
    assert legacy_false.enabled is False
    assert granted_but_vetoed.decision is ConsentDecision.GRANTED
    assert granted_but_vetoed.forced_off is True
    assert granted_but_vetoed.forced_off_reasons == (
        "config:privacy.disable_network_observability",
    )


@pytest.mark.parametrize(
    "environment",
    [
        {"DO_NOT_TRACK": "1"},
        {"CI": "true"},
        {"GITHUB_ACTIONS": "true"},
        {"PYTEST_CURRENT_TEST": "suite::test"},
        {"OPENSQUILLA_TESTING": "yes"},
        {"OPENSQUILLA_TELEMETRY_DISABLED": "on"},
        {"OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY": "true"},
    ],
)
def test_shared_environment_vetoes_force_both_scopes_off(
    environment: dict[str, str],
) -> None:
    config = _config(
        reliability_diagnostics_enabled=True,
        reliability_notice_version=VALID_NOTICE,
        reliability_consented_at_utc=VALID_CONSENT_TIME,
        product_analytics_enabled=True,
        product_analytics_notice_version=CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
        product_analytics_consented_at_utc=VALID_CONSENT_TIME,
    )

    for scope in TelemetryScope:
        state = resolve_scope_consent(scope, config=config, env=environment)
        assert state.decision is ConsentDecision.GRANTED
        assert state.forced_off is True
        assert state.enabled is False


def test_scope_specific_environment_veto_does_not_cross_scopes() -> None:
    config = _config(
        reliability_diagnostics_enabled=True,
        reliability_notice_version=VALID_NOTICE,
        reliability_consented_at_utc=VALID_CONSENT_TIME,
        product_analytics_enabled=True,
        product_analytics_notice_version=VALID_NOTICE,
        product_analytics_consented_at_utc=VALID_CONSENT_TIME,
    )
    env = {"OPENSQUILLA_PRIVACY_DISABLE_PRODUCT_ANALYTICS": "1"}

    assert resolve_scope_consent(TelemetryScope.RELIABILITY, config=config, env=env).enabled
    growth = resolve_scope_consent(TelemetryScope.GROWTH, config=config, env=env)
    assert growth.enabled is False
    assert growth.forced_off_reasons == ("env:OPENSQUILLA_PRIVACY_DISABLE_PRODUCT_ANALYTICS",)


def test_update_check_disable_does_not_veto_scoped_telemetry() -> None:
    config = _config(
        reliability_diagnostics_enabled=True,
        reliability_notice_version=CURRENT_RELIABILITY_NOTICE_VERSION,
        reliability_consented_at_utc=VALID_CONSENT_TIME,
        product_analytics_enabled=True,
        product_analytics_notice_version=CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
        product_analytics_consented_at_utc=VALID_CONSENT_TIME,
    )

    for scope in TelemetryScope:
        assert resolve_scope_consent(
            scope,
            config=config,
            env={"OPENSQUILLA_UPDATE_CHECK_DISABLED": "1"},
        ).enabled


def test_transient_pause_does_not_rewrite_persistent_consent_semantics() -> None:
    state = resolve_scope_consent(
        TelemetryScope.RELIABILITY,
        config=_config(
            reliability_diagnostics_enabled=True,
            reliability_notice_version=VALID_NOTICE,
            reliability_consented_at_utc=VALID_CONSENT_TIME,
        ),
        env={},
        transient_forced_off=True,
        transient_reason="collector_kill_switch",
    )

    assert state.decision is ConsentDecision.GRANTED
    assert state.persistently_disabled is False
    assert state.record_complete is True
    assert state.enabled is False
    assert state.forced_off_reasons == ("transient:collector_kill_switch",)
    assert state.local_state_directive is LocalStateDirective.KEEP


def test_persistent_decline_directs_only_its_scope_to_be_wiped() -> None:
    declined = resolve_scope_consent(
        TelemetryScope.GROWTH,
        config=_config(product_analytics_enabled=False),
        env={"CI": "true"},
    )
    reliability = resolve_scope_consent(
        TelemetryScope.RELIABILITY,
        config=_config(product_analytics_enabled=False),
        env={},
    )

    assert declined.local_state_directive is LocalStateDirective.WIPE_SCOPE
    assert declined.scope is TelemetryScope.GROWTH
    assert reliability.local_state_directive is LocalStateDirective.KEEP


def test_enqueue_and_send_recheck_the_same_fail_closed_policy() -> None:
    config = _config(
        reliability_diagnostics_enabled=True,
        reliability_notice_version=VALID_NOTICE,
        reliability_consented_at_utc=VALID_CONSENT_TIME,
    )

    state = resolve_scope_consent(TelemetryScope.RELIABILITY, config=config, env={})
    assert state.allowed_at(ConsentCheckpoint.ENQUEUE)
    assert state.allowed_at(ConsentCheckpoint.SEND)
    assert scope_enqueue_enabled(TelemetryScope.RELIABILITY, config=config, env={})
    assert scope_send_enabled(TelemetryScope.RELIABILITY, config=config, env={})

    forced_env = {"DO_NOT_TRACK": "1"}
    assert not scope_enqueue_enabled(
        TelemetryScope.RELIABILITY,
        config=config,
        env=forced_env,
    )
    assert not scope_send_enabled(
        TelemetryScope.RELIABILITY,
        config=config,
        env=forced_env,
    )


def test_unknown_consent_checkpoint_fails_closed() -> None:
    state = resolve_scope_consent(TelemetryScope.GROWTH, config=_config(), env={})

    with pytest.raises(ValueError):
        state.allowed_at("upload_later")


def test_invalid_scope_is_rejected_by_network_policy() -> None:
    with pytest.raises(ValueError, match="telemetry scope"):
        telemetry_scope_forced_off_reasons("unknown", env={})
