"""Shared privacy policy for non-user-initiated network observability."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

NETWORK_OBSERVABILITY_DISABLED_ENV = (
    "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY"
)
RELIABILITY_DIAGNOSTICS_DISABLED_ENV = (
    "OPENSQUILLA_PRIVACY_DISABLE_RELIABILITY_DIAGNOSTICS"
)
PRODUCT_ANALYTICS_DISABLED_ENV = "OPENSQUILLA_PRIVACY_DISABLE_PRODUCT_ANALYTICS"
LEGACY_TELEMETRY_DISABLED_ENV = "OPENSQUILLA_TELEMETRY_DISABLED"
LEGACY_UPDATE_CHECK_DISABLED_ENV = "OPENSQUILLA_UPDATE_CHECK_DISABLED"
DO_NOT_TRACK_ENV = "DO_NOT_TRACK"

_DISABLE_ENV_VARS = (
    NETWORK_OBSERVABILITY_DISABLED_ENV,
    LEGACY_TELEMETRY_DISABLED_ENV,
    LEGACY_UPDATE_CHECK_DISABLED_ENV,
)
_PROVIDER_INSTALL_ID_DISABLE_ENV_VARS = (
    NETWORK_OBSERVABILITY_DISABLED_ENV,
    LEGACY_TELEMETRY_DISABLED_ENV,
)
_SCOPED_TELEMETRY_DISABLE_ENV_VARS = (
    NETWORK_OBSERVABILITY_DISABLED_ENV,
    LEGACY_TELEMETRY_DISABLED_ENV,
)
_AUTO_SUPPRESS_ENV_VARS = (
    "GITHUB_ACTIONS",
    "PYTEST_CURRENT_TEST",
    "OPENSQUILLA_TESTING",
)
_SCOPED_TELEMETRY_AUTO_SUPPRESS_ENV_VARS = ("CI", *_AUTO_SUPPRESS_ENV_VARS)
_TELEMETRY_SCOPE_DISABLE_ENV = {
    "reliability": RELIABILITY_DIAGNOSTICS_DISABLED_ENV,
    "growth": PRODUCT_ANALYTICS_DISABLED_ENV,
}
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
    """Return whether the legacy install-id resolver is privacy-suppressed.

    The public TokenRhythm install-id helpers are retired no-ops.  This policy
    remains for private, read-only compatibility tests of existing local state;
    it is not a production authorization path.
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


def telemetry_scope_forced_off_reasons(
    scope: str,
    *,
    config: Any | None = None,
    env: Mapping[str, str | None] | None = None,
) -> tuple[str, ...]:
    """Return transient/global vetoes for one scoped telemetry stream.

    This function intentionally does not inspect the scope's persisted consent
    value.  A user decision of ``False`` is durable opt-out state, whereas the
    reasons returned here are effective-policy vetoes that must not rewrite or
    erase that decision.  Callers can therefore pause for CI or a remote/global
    kill switch and later resume only an independently valid consent record.
    """

    normalized_scope = str(scope).strip().lower()
    scope_disable_env = _TELEMETRY_SCOPE_DISABLE_ENV.get(normalized_scope)
    if scope_disable_env is None:
        valid = ", ".join(sorted(_TELEMETRY_SCOPE_DISABLE_ENV))
        raise ValueError(f"telemetry scope must be one of {{{valid}}}")

    env_source = os.environ if env is None else env
    reasons: list[str] = []
    if _config_disables_network_observability(config):
        reasons.append("config:privacy.disable_network_observability")
    for name in _SCOPED_TELEMETRY_DISABLE_ENV_VARS:
        if _is_truthy(env_source.get(name)):
            reasons.append(f"env:{name}")
    if _is_truthy(env_source.get(scope_disable_env)):
        reasons.append(f"env:{scope_disable_env}")
    if _is_truthy(env_source.get(DO_NOT_TRACK_ENV)):
        reasons.append(f"env:{DO_NOT_TRACK_ENV}")
    for name in _SCOPED_TELEMETRY_AUTO_SUPPRESS_ENV_VARS:
        if _automated_environment_value_is_active(name, env_source.get(name)):
            reasons.append(f"environment:{name}")
    return tuple(dict.fromkeys(reasons))


def _automated_environment_value_is_active(name: str, value: object) -> bool:
    if name == "PYTEST_CURRENT_TEST":
        return isinstance(value, str) and bool(value.strip())
    return _is_truthy(value)


def _config_disables_network_observability(config: Any | None) -> bool:
    privacy = getattr(config, "privacy", None)
    disabled = getattr(privacy, "disable_network_observability", False)
    if isinstance(disabled, str):
        return _is_truthy(disabled)
    return bool(disabled)


def _is_truthy(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in _TRUE_VALUES
