from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.env import load_env
from opensquilla.gateway.config import GatewayConfig, PrivacyConfig

_SCOPED_CONSENT_ENV_VALUES = {
    "reliability_diagnostics_enabled": "true",
    "reliability_notice_version": "untrusted-notice",
    "reliability_consented_at_utc": "2026-09-01T08:30:00.000Z",
    "product_analytics_enabled": "true",
    "product_analytics_notice_version": "untrusted-notice",
    "product_analytics_consented_at_utc": "2026-09-01T08:30:00.000Z",
}


@pytest.mark.parametrize(("field_name", "env_value"), _SCOPED_CONSENT_ENV_VALUES.items())
def test_direct_privacy_environment_cannot_manufacture_scoped_consent(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    env_value: str,
) -> None:
    monkeypatch.setenv(f"OPENSQUILLA_PRIVACY_{field_name.upper()}", env_value)

    assert getattr(PrivacyConfig(), field_name) is None
    assert getattr(GatewayConfig().privacy, field_name) is None


@pytest.mark.parametrize(("field_name", "env_value"), _SCOPED_CONSENT_ENV_VALUES.items())
def test_nested_gateway_environment_cannot_manufacture_scoped_consent(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    env_value: str,
) -> None:
    monkeypatch.setenv(f"OPENSQUILLA_GATEWAY_PRIVACY__{field_name.upper()}", env_value)

    assert getattr(GatewayConfig().privacy, field_name) is None


@pytest.mark.parametrize(
    "field_name",
    ("reliability_diagnostics_enabled", "product_analytics_enabled"),
)
@pytest.mark.parametrize("env_value", ("true", "false"))
def test_environment_cannot_manufacture_grant_or_decline(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    env_value: str,
) -> None:
    monkeypatch.setenv(f"OPENSQUILLA_PRIVACY_{field_name.upper()}", env_value)
    monkeypatch.setenv(
        f"OPENSQUILLA_GATEWAY_PRIVACY__{field_name.upper()}",
        env_value,
    )

    assert getattr(PrivacyConfig(), field_name) is None
    assert getattr(GatewayConfig().privacy, field_name) is None


def test_dotenv_sources_cannot_manufacture_scoped_consent(tmp_path: Path) -> None:
    direct_env = tmp_path / "direct.env"
    direct_env.write_text(
        "\n".join(
            f"OPENSQUILLA_PRIVACY_{field_name.upper()}={env_value}"
            for field_name, env_value in _SCOPED_CONSENT_ENV_VALUES.items()
        ),
        encoding="utf-8",
    )
    nested_env = tmp_path / "nested.env"
    nested_env.write_text(
        "\n".join(
            f"OPENSQUILLA_GATEWAY_PRIVACY__{field_name.upper()}={env_value}"
            for field_name, env_value in _SCOPED_CONSENT_ENV_VALUES.items()
        ),
        encoding="utf-8",
    )

    direct = PrivacyConfig(_env_file=direct_env)
    nested = GatewayConfig(_env_file=nested_env).privacy

    for field_name in _SCOPED_CONSENT_ENV_VALUES:
        assert getattr(direct, field_name) is None
        assert getattr(nested, field_name) is None


def test_project_dotenv_loader_cannot_manufacture_scoped_consent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = (
        "OPENSQUILLA_PRIVACY_RELIABILITY_DIAGNOSTICS_ENABLED",
        "OPENSQUILLA_PRIVACY_RELIABILITY_NOTICE_VERSION",
        "OPENSQUILLA_PRIVACY_RELIABILITY_CONSENTED_AT_UTC",
        "OPENSQUILLA_GATEWAY_PRIVACY__PRODUCT_ANALYTICS_ENABLED",
        "OPENSQUILLA_GATEWAY_PRIVACY__PRODUCT_ANALYTICS_NOTICE_VERSION",
        "OPENSQUILLA_GATEWAY_PRIVACY__PRODUCT_ANALYTICS_CONSENTED_AT_UTC",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                f"{names[0]}=false",
                f"{names[1]}=untrusted-notice",
                f"{names[2]}=2026-09-01T08:30:00.000Z",
                f"{names[3]}=true",
                f"{names[4]}=untrusted-notice",
                f"{names[5]}=2026-09-01T08:30:00.000Z",
            )
        ),
        encoding="utf-8",
    )

    assert load_env(cwd=tmp_path, include_home=False) == len(names)

    privacy = GatewayConfig().privacy
    assert privacy.reliability_diagnostics_enabled is None
    assert privacy.reliability_notice_version is None
    assert privacy.reliability_consented_at_utc is None
    assert privacy.product_analytics_enabled is None
    assert privacy.product_analytics_notice_version is None
    assert privacy.product_analytics_consented_at_utc is None


def test_legacy_environment_switches_remain_effective_vetoes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY", "true")
    assert PrivacyConfig().disable_network_observability is True
    assert GatewayConfig().privacy.disable_network_observability is True
    monkeypatch.delenv("OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY")

    monkeypatch.setenv(
        "OPENSQUILLA_GATEWAY_PRIVACY__DISABLE_NETWORK_OBSERVABILITY",
        "true",
    )
    assert GatewayConfig().privacy.disable_network_observability is True
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_PRIVACY__DISABLE_NETWORK_OBSERVABILITY")

    direct_env = tmp_path / "direct.env"
    direct_env.write_text(
        "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY=true\n",
        encoding="utf-8",
    )
    nested_env = tmp_path / "nested.env"
    nested_env.write_text(
        "OPENSQUILLA_GATEWAY_PRIVACY__DISABLE_NETWORK_OBSERVABILITY=true\n",
        encoding="utf-8",
    )
    assert PrivacyConfig(_env_file=direct_env).disable_network_observability is True
    assert GatewayConfig(_env_file=nested_env).privacy.disable_network_observability is True


def test_authenticated_config_input_can_persist_scoped_consent() -> None:
    privacy = GatewayConfig(
        privacy={
            "reliability_diagnostics_enabled": True,
            "reliability_notice_version": "reliability-v1",
            "reliability_consented_at_utc": "2026-09-01T08:30:00.000Z",
        }
    ).privacy

    assert privacy.reliability_diagnostics_enabled is True
    assert privacy.reliability_notice_version == "reliability-v1"
    assert privacy.reliability_consented_at_utc == "2026-09-01T08:30:00.000Z"
