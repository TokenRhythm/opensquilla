"""Unified upload preference with independent runtime vetoes for each stream.

The public consent-shaped state remains compatible with existing producers.
Its allowed state expresses upload policy, not a newly authored consent record.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from opensquilla.observability.network_policy import telemetry_scope_forced_off_reasons
from opensquilla.telemetry.contracts.manifest import CURRENT_NOTICE_VERSION_BY_SCOPE


class TelemetryScope(StrEnum):
    RELIABILITY = "reliability"
    GROWTH = "growth"


class ConsentDecision(StrEnum):
    UNSET = "unset"
    GRANTED = "granted"
    DECLINED = "declined"


class ConsentCheckpoint(StrEnum):
    """Network-data boundary at which consent must be re-evaluated."""

    ENQUEUE = "enqueue"
    SEND = "send"


class LocalStateDirective(StrEnum):
    """Action a consent transition requires for one scope's local state."""

    KEEP = "keep"
    WIPE_SCOPE = "wipe_scope"


CURRENT_RELIABILITY_NOTICE_VERSION = CURRENT_NOTICE_VERSION_BY_SCOPE["reliability"]
CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION = CURRENT_NOTICE_VERSION_BY_SCOPE["growth"]

_CURRENT_NOTICE_VERSIONS = {
    TelemetryScope.RELIABILITY: CURRENT_RELIABILITY_NOTICE_VERSION,
    TelemetryScope.GROWTH: CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
}


@dataclass(frozen=True)
class ScopeConsentState:
    """One scope's persisted decision plus its current effective policy."""

    scope: TelemetryScope
    decision: ConsentDecision
    notice_version: str | None
    consented_at_utc: str | None
    record_complete: bool
    notice_current: bool
    forced_off_reasons: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return (
            self.decision is ConsentDecision.GRANTED
            and not self.forced_off_reasons
        )

    @property
    def forced_off(self) -> bool:
        return bool(self.forced_off_reasons)

    @property
    def persistently_disabled(self) -> bool:
        return self.decision is ConsentDecision.DECLINED

    @property
    def local_state_directive(self) -> LocalStateDirective:
        """The unified V1 preference pauses retained telemetry state."""

        return LocalStateDirective.KEEP

    def allowed_at(self, checkpoint: ConsentCheckpoint | str) -> bool:
        """Return fail-closed permission at an enqueue or send boundary."""

        ConsentCheckpoint(checkpoint)
        return self.enabled

    @property
    def enqueue_allowed(self) -> bool:
        return self.allowed_at(ConsentCheckpoint.ENQUEUE)

    @property
    def send_allowed(self) -> bool:
        return self.allowed_at(ConsentCheckpoint.SEND)

    @property
    def block_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.decision is ConsentDecision.UNSET:
            reasons.append("consent:unset")
        elif self.decision is ConsentDecision.DECLINED:
            reasons.append("consent:declined")
        reasons.extend(self.forced_off_reasons)
        return tuple(reasons)


def resolve_scope_consent(
    scope: TelemetryScope | str,
    *,
    config: Any | None = None,
    env: Mapping[str, str | None] | None = None,
    required_notice_version: str | None = None,
    transient_forced_off: bool = False,
    transient_reason: str = "remote_policy",
) -> ScopeConsentState:
    """Use the V1 global opt-out policy at both collection and upload boundaries.

    Old scoped declines remain a total opt-out until config migration retires
    those fields. Notice versions describe the event protocol and never require
    an additional user prompt or a fabricated consent timestamp.
    """

    normalized_scope = TelemetryScope(scope)
    privacy = getattr(config, "privacy", None)
    disabled = (
        getattr(privacy, "disable_network_observability", False) is True
        or getattr(privacy, "reliability_diagnostics_enabled", None) is False
        or getattr(privacy, "product_analytics_enabled", None) is False
    )
    decision = ConsentDecision.DECLINED if disabled else ConsentDecision.GRANTED
    notice_version = _CURRENT_NOTICE_VERSIONS[normalized_scope]
    # Retained call signature for older callers; authorization no longer depends
    # on a consent-record version. Event schemas enforce their own versions.
    del required_notice_version
    forced_reasons = list(
        telemetry_scope_forced_off_reasons(
            normalized_scope.value,
            config=config,
            env=env,
        )
    )
    if transient_forced_off:
        reason = _nonempty_string(transient_reason) or "policy"
        forced_reasons.append(f"transient:{reason}")

    return ScopeConsentState(
        scope=normalized_scope,
        decision=decision,
        notice_version=notice_version,
        consented_at_utc=None,
        record_complete=not disabled,
        notice_current=True,
        forced_off_reasons=tuple(dict.fromkeys(forced_reasons)),
    )


def scope_collection_enabled(
    scope: TelemetryScope | str,
    *,
    config: Any | None = None,
    env: Mapping[str, str | None] | None = None,
    required_notice_version: str | None = None,
    transient_forced_off: bool = False,
) -> bool:
    """Return enqueue permission for compatibility with collection callers."""

    return scope_enqueue_enabled(
        scope,
        config=config,
        env=env,
        required_notice_version=required_notice_version,
        transient_forced_off=transient_forced_off,
    )


def scope_enqueue_enabled(
    scope: TelemetryScope | str,
    *,
    config: Any | None = None,
    env: Mapping[str, str | None] | None = None,
    required_notice_version: str | None = None,
    transient_forced_off: bool = False,
) -> bool:
    """Re-evaluate consent immediately before durable local collection."""

    return resolve_scope_consent(
        scope,
        config=config,
        env=env,
        required_notice_version=required_notice_version,
        transient_forced_off=transient_forced_off,
    ).enqueue_allowed


def scope_send_enabled(
    scope: TelemetryScope | str,
    *,
    config: Any | None = None,
    env: Mapping[str, str | None] | None = None,
    required_notice_version: str | None = None,
    transient_forced_off: bool = False,
) -> bool:
    """Re-evaluate consent immediately before an upload request starts."""

    return resolve_scope_consent(
        scope,
        config=config,
        env=env,
        required_notice_version=required_notice_version,
        transient_forced_off=transient_forced_off,
    ).send_allowed


def _nonempty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
