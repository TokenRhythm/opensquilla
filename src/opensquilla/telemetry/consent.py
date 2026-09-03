"""Effective consent policy for isolated telemetry scopes.

Persisted choices and runtime vetoes are kept separate on purpose: a CI or
kill-switch pause must never manufacture consent, erase an explicit decline,
or mutate the operator's saved decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
            and self.record_complete
            and self.notice_current
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
        """Return the narrow cleanup action implied by persisted consent.

        Only an explicit, persisted decline authorizes deletion.  Runtime
        vetoes such as CI, DNT, or a remote pause merely stop collection and
        sending; they must leave the independently consented scope's queue and
        identity untouched.
        """

        if self.persistently_disabled:
            return LocalStateDirective.WIPE_SCOPE
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
        elif not self.record_complete:
            reasons.append("consent:incomplete")
        elif not self.notice_current:
            reasons.append("consent:notice_stale")
        reasons.extend(self.forced_off_reasons)
        return tuple(reasons)


_SCOPE_CONFIG_FIELDS = {
    TelemetryScope.RELIABILITY: (
        "reliability_diagnostics_enabled",
        "reliability_notice_version",
        "reliability_consented_at_utc",
    ),
    TelemetryScope.GROWTH: (
        "product_analytics_enabled",
        "product_analytics_notice_version",
        "product_analytics_consented_at_utc",
    ),
}


def resolve_scope_consent(
    scope: TelemetryScope | str,
    *,
    config: Any | None = None,
    env: Mapping[str, str | None] | None = None,
    required_notice_version: str | None = None,
    transient_forced_off: bool = False,
    transient_reason: str = "remote_policy",
) -> ScopeConsentState:
    """Resolve whether one telemetry scope may collect or upload now.

    Only the literal boolean ``True`` is a grant.  Missing metadata, malformed
    UTC timestamps, stale notices, legacy/global privacy switches, DNT, and
    automated environments all fail closed without changing saved consent.
    """

    normalized_scope = TelemetryScope(scope)
    enabled_field, notice_field, timestamp_field = _SCOPE_CONFIG_FIELDS[normalized_scope]
    privacy = getattr(config, "privacy", None)
    configured_enabled = getattr(privacy, enabled_field, None)
    notice_version = _nonempty_string(getattr(privacy, notice_field, None))
    consented_at_utc = _nonempty_string(getattr(privacy, timestamp_field, None))

    if configured_enabled is True:
        decision = ConsentDecision.GRANTED
    elif configured_enabled is False:
        decision = ConsentDecision.DECLINED
    else:
        decision = ConsentDecision.UNSET

    record_complete = (
        decision is ConsentDecision.GRANTED
        and notice_version is not None
        and _is_utc_timestamp(consented_at_utc)
    )
    required_notice = (
        _nonempty_string(required_notice_version) or _CURRENT_NOTICE_VERSIONS[normalized_scope]
    )
    notice_current = notice_version == required_notice
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
        consented_at_utc=consented_at_utc,
        record_complete=record_complete,
        notice_current=notice_current,
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


def _is_utc_timestamp(value: str | None) -> bool:
    if value is None:
        return False
    candidate = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed)
