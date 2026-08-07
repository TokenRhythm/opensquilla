"""Shared privacy policy for non-user-initiated network observability."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

NETWORK_OBSERVABILITY_DISABLED_ENV = (
    "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY"
)
LEGACY_TELEMETRY_DISABLED_ENV = "OPENSQUILLA_TELEMETRY_DISABLED"
LEGACY_UPDATE_CHECK_DISABLED_ENV = "OPENSQUILLA_UPDATE_CHECK_DISABLED"

_DISABLE_ENV_VARS = (
    NETWORK_OBSERVABILITY_DISABLED_ENV,
    LEGACY_TELEMETRY_DISABLED_ENV,
    LEGACY_UPDATE_CHECK_DISABLED_ENV,
)
_PROVIDER_INSTALL_ID_DISABLE_ENV_VARS = (
    NETWORK_OBSERVABILITY_DISABLED_ENV,
    LEGACY_TELEMETRY_DISABLED_ENV,
)
_AUTO_SUPPRESS_ENV_VARS = (
    "GITHUB_ACTIONS",
    "PYTEST_CURRENT_TEST",
    "OPENSQUILLA_TESTING",
)
_TRUE_VALUES = {"1", "true", "yes", "on"}


def network_observability_disabled(
    *,
    config: Any | None = None,
    env: Mapping[str, str | None] | None = None,
) -> bool:
    """Return whether passive telemetry/update network checks are disabled."""
    env_source = os.environ if env is None else env
    if any(_is_truthy(env_source.get(name)) for name in _DISABLE_ENV_VARS):
        return True
    return _config_disables_network_observability(config)


def provider_request_correlation_disabled(
    *,
    config: Any | None = None,
    env: Mapping[str, str | None] | None = None,
) -> bool:
    """Return whether provider-bound request correlation is disabled.

    Provider correlation follows only the dedicated privacy switch.  Legacy
    telemetry and update-check switches intentionally remain scoped to their
    historical passive-network behavior.
    """

    env_source = os.environ if env is None else env
    if _is_truthy(env_source.get(NETWORK_OBSERVABILITY_DISABLED_ENV)):
        return True
    return _config_disables_network_observability(config)


def provider_install_id_disabled(
    *,
    config: Any | None = None,
    env: Mapping[str, str | None] | None = None,
) -> bool:
    """Return whether the TokenRhythm installation identifier is disabled.

    The installation identifier is passive telemetry metadata even though it
    travels with a provider request.  It therefore honors both the unified
    privacy switch and the legacy telemetry switch, but intentionally does not
    inherit the update-check-only switch.  Automated CI/test environments use
    the same suppression rules as installation telemetry so they neither
    create telemetry state nor emit an identifier accidentally.
    """

    env_source = os.environ if env is None else env
    if any(
        _is_truthy(env_source.get(name))
        for name in _PROVIDER_INSTALL_ID_DISABLE_ENV_VARS
    ):
        return True
    if _config_disables_network_observability(config):
        return True
    return _provider_install_id_environment_suppressed(env_source)


def _provider_install_id_environment_suppressed(
    env: Mapping[str, str | None],
) -> bool:
    for name in _AUTO_SUPPRESS_ENV_VARS:
        value = env.get(name)
        if name == "PYTEST_CURRENT_TEST":
            if isinstance(value, str) and value.strip():
                return True
            continue
        if _is_truthy(value):
            return True
    return False


def _config_disables_network_observability(config: Any | None) -> bool:
    privacy = getattr(config, "privacy", None)
    disabled = getattr(privacy, "disable_network_observability", False)
    if isinstance(disabled, str):
        return _is_truthy(disabled)
    return bool(disabled)


def _is_truthy(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in _TRUE_VALUES
