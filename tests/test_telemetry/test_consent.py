from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from opensquilla.gateway.config import GatewayConfig, PrivacyConfig
from opensquilla.gateway.config_migration import migrate_config_payload
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


def _config(**privacy: object) -> SimpleNamespace:
    return SimpleNamespace(privacy=SimpleNamespace(**privacy))


@pytest.mark.parametrize("scope", list(TelemetryScope))
def test_default_upload_policy_permits_both_streams_without_consent_record(
    scope: TelemetryScope,
) -> None:
    config = GatewayConfig()
    state = resolve_scope_consent(scope, config=config, env={})

    assert state.enabled
    assert state.decision is ConsentDecision.GRANTED
    assert state.consented_at_utc is None
    assert state.record_complete
    assert state.notice_current
    assert state.notice_version == {
        TelemetryScope.RELIABILITY: CURRENT_RELIABILITY_NOTICE_VERSION,
        TelemetryScope.GROWTH: CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
    }[scope]
    persisted = config.to_toml_dict()["privacy"]
    assert "reliability_diagnostics_enabled" not in persisted
    assert "product_analytics_enabled" not in persisted
    assert "product_analytics_consented_at_utc" not in persisted


@pytest.mark.parametrize("field", [
    "reliability_diagnostics_enabled",
    "product_analytics_enabled",
])
def test_legacy_decline_migrates_to_global_off_and_retires_scoped_keys(field: str) -> None:
    old_privacy = {
        field: False,
        "reliability_notice_version": "reliability-v1",
        "product_analytics_notice_version": "growth-v1",
        "product_analytics_consented_at_utc": "2026-09-01T08:00:00Z",
    }
    migrated = migrate_config_payload({"privacy": old_privacy}, emit_diagnostics=False)
    assert migrated.payload["privacy"] == {"disable_network_observability": True}
    assert old_privacy[field] is False

    privacy = PrivacyConfig(**old_privacy)
    assert privacy.disable_network_observability
    assert privacy.reliability_diagnostics_enabled is None
    assert privacy.product_analytics_enabled is None
    assert privacy.product_analytics_notice_version is None
    assert privacy.product_analytics_consented_at_utc is None
    for scope in TelemetryScope:
        state = resolve_scope_consent(scope, config=GatewayConfig(privacy=privacy), env={})
        assert not state.enabled
        assert state.local_state_directive is LocalStateDirective.KEEP


@pytest.mark.parametrize("value", [False, "false", "off", "0", 0, 0.0])
def test_disk_migration_preserves_all_legacy_false_boolean_spellings(value: object) -> None:
    migrated = migrate_config_payload(
        {"privacy": {"product_analytics_enabled": value}}, emit_diagnostics=False,
    )
    assert migrated.payload["privacy"] == {"disable_network_observability": True}


@pytest.mark.parametrize("value", ["invalid", 2, [], {}])
def test_disk_migration_rejects_invalid_legacy_boolean_values(value: object) -> None:
    with pytest.raises(ValidationError):
        migrate_config_payload(
            {"privacy": {"product_analytics_enabled": value}}, emit_diagnostics=False,
        )


@pytest.mark.parametrize("value", [None, True])
@pytest.mark.parametrize("metadata", [
    {},
    {"product_analytics_notice_version": "growth-v1"},
    {"product_analytics_notice_version": "old", "product_analytics_consented_at_utc": "bad"},
])
def test_missing_or_stale_metadata_never_requires_separate_reconsent(
    value: bool | None,
    metadata: dict[str, object],
) -> None:
    config = _config(product_analytics_enabled=value, **metadata)
    for scope in TelemetryScope:
        state = resolve_scope_consent(scope, config=config, env={})
        assert state.enabled
        assert state.block_reasons == ()
        assert state.consented_at_utc is None


def test_old_notice_requirement_does_not_change_current_event_protocol() -> None:
    state = resolve_scope_consent(
        TelemetryScope.GROWTH,
        config=_config(),
        env={},
        required_notice_version="growth-v1",
    )
    assert state.enabled
    assert state.notice_version == CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION


@pytest.mark.parametrize("scope", list(TelemetryScope))
def test_global_opt_out_pauses_both_enqueue_and_send_and_retains_state(
    scope: TelemetryScope,
) -> None:
    config = _config(disable_network_observability=False)
    assert scope_collection_enabled(scope, config=config, env={})
    config.privacy.disable_network_observability = True
    assert not scope_enqueue_enabled(scope, config=config, env={})
    assert not scope_send_enabled(scope, config=config, env={})
    state = resolve_scope_consent(scope, config=config, env={})
    assert state.persistently_disabled
    assert state.local_state_directive is LocalStateDirective.KEEP
    config.privacy.disable_network_observability = False
    assert scope_enqueue_enabled(scope, config=config, env={})
    assert scope_send_enabled(scope, config=config, env={})


@pytest.mark.parametrize("field", [
    "reliability_diagnostics_enabled",
    "product_analytics_enabled",
])
def test_unmigrated_legacy_decline_also_blocks_both_streams(field: str) -> None:
    for scope in TelemetryScope:
        state = resolve_scope_consent(scope, config=_config(**{field: False}), env={})
        assert not state.enabled
        assert state.local_state_directive is LocalStateDirective.KEEP


@pytest.mark.parametrize("environment", [
    {"DO_NOT_TRACK": "1"},
    {"CI": "true"},
    {"GITHUB_ACTIONS": "true"},
    {"PYTEST_CURRENT_TEST": "suite::test"},
    {"OPENSQUILLA_TESTING": "yes"},
    {"OPENSQUILLA_TELEMETRY_DISABLED": "on"},
    {"OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY": "true"},
])
def test_environment_vetoes_still_block_default_enabled_uploads(
    environment: dict[str, str],
) -> None:
    for scope in TelemetryScope:
        state = resolve_scope_consent(scope, config=_config(), env=environment)
        assert not state.enabled
        assert state.forced_off
        assert state.local_state_directive is LocalStateDirective.KEEP
        assert state.consented_at_utc is None


def test_legacy_scope_specific_environment_veto_remains_compatible() -> None:
    env = {"OPENSQUILLA_PRIVACY_DISABLE_PRODUCT_ANALYTICS": "1"}
    assert resolve_scope_consent(TelemetryScope.RELIABILITY, env=env).enabled
    assert not resolve_scope_consent(TelemetryScope.GROWTH, env=env).enabled


def test_update_check_disable_does_not_veto_telemetry() -> None:
    for scope in TelemetryScope:
        assert resolve_scope_consent(scope, env={"OPENSQUILLA_UPDATE_CHECK_DISABLED": "1"}).enabled


def test_transient_remote_pause_retains_saved_preference_and_state() -> None:
    state = resolve_scope_consent(
        TelemetryScope.RELIABILITY,
        config=_config(),
        env={},
        transient_forced_off=True,
        transient_reason="collector_kill_switch",
    )
    assert state.decision is ConsentDecision.GRANTED
    assert state.forced_off_reasons == ("transient:collector_kill_switch",)
    assert not state.enabled
    assert state.local_state_directive is LocalStateDirective.KEEP


def test_unknown_checkpoint_or_scope_remains_invalid() -> None:
    state = resolve_scope_consent(TelemetryScope.GROWTH, env={})
    assert state.allowed_at(ConsentCheckpoint.ENQUEUE)
    with pytest.raises(ValueError):
        state.allowed_at("upload_later")
    with pytest.raises(ValueError, match="telemetry scope"):
        telemetry_scope_forced_off_reasons("unknown", env={})
