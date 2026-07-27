from __future__ import annotations

from opensquilla.gateway.config import GatewayConfig, PrivacyConfig
from opensquilla.observability.network_policy import (
    network_observability_disabled,
    provider_request_correlation_disabled,
)

GLOBAL_DISABLE_ENV = "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY"
TELEMETRY_DISABLED_ENV = "OPENSQUILLA_TELEMETRY_DISABLED"
UPDATE_CHECK_DISABLED_ENV = "OPENSQUILLA_UPDATE_CHECK_DISABLED"


def test_defaults_allow_network_observability() -> None:
    assert network_observability_disabled(env={}) is False


def test_config_disable_disables_network_observability() -> None:
    config = GatewayConfig(
        privacy=PrivacyConfig(disable_network_observability=True),
    )

    assert network_observability_disabled(config=config, env={}) is True


def test_new_privacy_env_disables_network_observability() -> None:
    assert (
        network_observability_disabled(
            env={GLOBAL_DISABLE_ENV: "On"},
        )
        is True
    )


def test_legacy_telemetry_env_disables_network_observability() -> None:
    assert (
        network_observability_disabled(
            env={TELEMETRY_DISABLED_ENV: "TRUE"},
        )
        is True
    )


def test_legacy_update_check_env_disables_network_observability() -> None:
    assert (
        network_observability_disabled(
            env={UPDATE_CHECK_DISABLED_ENV: "yes"},
        )
        is True
    )


def test_provider_correlation_ignores_legacy_network_disable_envs() -> None:
    assert (
        provider_request_correlation_disabled(
            env={
                TELEMETRY_DISABLED_ENV: "true",
                UPDATE_CHECK_DISABLED_ENV: "true",
            },
        )
        is False
    )


def test_provider_correlation_honors_dedicated_env() -> None:
    assert (
        provider_request_correlation_disabled(
            env={GLOBAL_DISABLE_ENV: "yes"},
        )
        is True
    )


def test_provider_correlation_honors_config_without_base_settings() -> None:
    class _Privacy:
        disable_network_observability = "on"

    class _Config:
        privacy = _Privacy()

    assert provider_request_correlation_disabled(config=_Config(), env={}) is True


def test_false_env_does_not_override_config_disable() -> None:
    config = GatewayConfig(
        privacy=PrivacyConfig(disable_network_observability=True),
    )

    assert (
        network_observability_disabled(
            config=config,
            env={
                GLOBAL_DISABLE_ENV: "false",
                TELEMETRY_DISABLED_ENV: "0",
                UPDATE_CHECK_DISABLED_ENV: "off",
            },
        )
        is True
    )


def test_gateway_public_config_does_not_expose_legacy_disable_as_unified(
    monkeypatch,
) -> None:
    monkeypatch.setenv(UPDATE_CHECK_DISABLED_ENV, "1")
    config = GatewayConfig(
        privacy=PrivacyConfig(disable_network_observability=False),
    )

    public = config.to_public_dict()

    assert public["privacy"]["disable_network_observability"] is False
    assert public["privacy"]["network_observability_disabled_effective"] is False


def test_gateway_public_config_exposes_effective_dedicated_privacy_env(
    monkeypatch,
) -> None:
    monkeypatch.setenv(GLOBAL_DISABLE_ENV, "1")
    config = GatewayConfig(
        privacy=PrivacyConfig(disable_network_observability=False),
    )

    public = config.to_public_dict()

    assert public["privacy"]["disable_network_observability"] is False
    assert public["privacy"]["network_observability_disabled_effective"] is True
