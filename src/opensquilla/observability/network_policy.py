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


def _config_disables_network_observability(config: Any | None) -> bool:
    privacy = getattr(config, "privacy", None)
    disabled = getattr(privacy, "disable_network_observability", False)
    if isinstance(disabled, str):
        return _is_truthy(disabled)
    return bool(disabled)


def _is_truthy(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in _TRUE_VALUES
