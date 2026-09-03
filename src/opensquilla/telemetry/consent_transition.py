"""Shared fail-closed boundary for telemetry-affecting config transitions."""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from opensquilla.paths import default_opensquilla_home
from opensquilla.telemetry.consent import (
    ConsentDecision,
    ScopeConsentState,
    TelemetryScope,
    resolve_scope_consent,
)
from opensquilla.telemetry.coordination import scope_consent_coordinator_for
from opensquilla.telemetry.desktop_state import write_desktop_consent_mirror

_TRANSITION_SCOPE_ORDER = (
    TelemetryScope.RELIABILITY,
    TelemetryScope.GROWTH,
)


def telemetry_state_dir(config: Any) -> str | Path:
    """Return the exact state root shared by Gateway and Desktop telemetry."""

    configured = getattr(config, "state_dir", None)
    if isinstance(configured, str) and configured.strip():
        return configured
    if isinstance(configured, Path):
        return configured
    return default_opensquilla_home() / "state"


def publish_desktop_consent_mirror(
    config: Any,
    *,
    fail_closed_scopes: Collection[TelemetryScope] = (),
) -> Path:
    """Publish one snapshot, optionally forcing selected scopes closed."""

    forced = frozenset(fail_closed_scopes)
    states = {
        scope: resolve_scope_consent(scope, config=config) for scope in _TRANSITION_SCOPE_ORDER
    }
    for scope in forced:
        states[scope] = ScopeConsentState(
            scope=scope,
            decision=ConsentDecision.DECLINED,
            notice_version=None,
            consented_at_utc=None,
            record_complete=False,
            notice_current=False,
            forced_off_reasons=("transition",),
        )
    return write_desktop_consent_mirror(
        telemetry_state_dir(config),
        reliability=states[TelemetryScope.RELIABILITY],
        growth=states[TelemetryScope.GROWTH],
    )


def _network_observability_disabled(config: Any | None) -> bool:
    privacy = getattr(config, "privacy", None)
    return getattr(privacy, "disable_network_observability", False) is True


@asynccontextmanager
async def global_network_observability_transition(
    current_config: Any | None,
    candidate_config: Any,
) -> AsyncIterator[None]:
    """Serialize a global telemetry veto change with both scope boundaries.

    The global switch is a pause, not consent withdrawal, so this transaction
    never deletes queues or identities. Both locks stay held from the first
    fail-closed Desktop publication through the durable/live caller commit and
    final mirror publication.
    """

    if current_config is None or _network_observability_disabled(
        current_config
    ) == _network_observability_disabled(candidate_config):
        yield
        return

    coordinator = scope_consent_coordinator_for(current_config)
    async with coordinator.transition(TelemetryScope.RELIABILITY):
        async with coordinator.transition(TelemetryScope.GROWTH):
            publish_desktop_consent_mirror(
                current_config,
                fail_closed_scopes=_TRANSITION_SCOPE_ORDER,
            )
            yield
            publish_desktop_consent_mirror(candidate_config)


__all__ = [
    "global_network_observability_transition",
    "publish_desktop_consent_mirror",
    "telemetry_state_dir",
]
