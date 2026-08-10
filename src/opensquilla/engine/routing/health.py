"""Provider deployment health ledger: temporary benching on classified failures.

Complements :mod:`opensquilla.engine.fallback`: the fallback policy decides
*retry-vs-surface* for one in-flight call, while this ledger answers a
different question — *is this (provider, model, upstream) deployment temporarily
benched?* — across calls and turns.

Bench rules (decision D13, pinned):

- A deployment is benched after ``failure_threshold`` (default 3) recorded
  benchable failures, for ``cooldown_s`` (default 30) seconds.
- ``RATE_LIMITED`` (HTTP 429) benches immediately; the cooldown is the
  provider's ``Retry-After`` when present, else the default.
- On 5xx-shaped failures (``PROVIDER_OVERLOADED`` / gateway-transient),
  ``Retry-After`` is honored for the cooldown when the bench triggers.
- The ledger NEVER reports the only viable deployment for a tier as benched:
  :meth:`ProviderHealthLedger.eligible` takes the candidate set and refuses
  to strand routing when every alternative is also benched.

The ledger is passive infrastructure: constructing it and never feeding it
changes nothing, and every consumer treats "no ledger" as "everything
eligible". Timekeeping mirrors ``CredentialPool``: an injectable
``clock: Callable[[], float]`` defaulting to ``time.monotonic``, so
wall-clock drift can never corrupt bench state, guarded by a
``threading.Lock``.

Log hygiene: bench/unbench events carry only the provider id, model id, the
:class:`~opensquilla.provider.failures.ProviderFailureKind` enum token, and
numeric cooldowns — never raw provider error text or credentials.
"""

from __future__ import annotations

import math
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Final

import structlog

from opensquilla.provider.deployment import (
    canonicalize_provider_routing_upstream,
)
from opensquilla.provider.failures import ProviderFailureKind

log = structlog.get_logger(__name__)

DEFAULT_FAILURE_THRESHOLD: Final[int] = 3
DEFAULT_COOLDOWN_S: Final[float] = 30.0
# Defensive ceiling for Retry-After-driven cooldowns: a broken or hostile
# header must not park a deployment for hours. Generous enough that any
# realistic provider hint is honored verbatim.
DEFAULT_MAX_COOLDOWN_S: Final[float] = 900.0
DEFAULT_TELEMETRY_TTL_S: Final[float] = 900.0
DEFAULT_OBSERVATION_WINDOW_S: Final[float] = 300.0
DEFAULT_OBSERVATION_WINDOW_SIZE: Final[int] = 50
DEFAULT_LATENCY_EWMA_ALPHA: Final[float] = 0.2

# Kinds that signal *deployment* unhealth. Request-shaped kinds
# (CONTEXT_OVERFLOW, BAD_REQUEST, POLICY_REFUSAL, UNSUPPORTED_FEATURE) follow
# the request, not the deployment; deterministic config kinds (AUTH_INVALID,
# INSUFFICIENT_CREDITS, MODEL_NOT_FOUND) are handled by
# ``decide_recovery_action`` (FAIL_CONFIG / FALLBACK_PROVIDER) and would not
# recover within a cooldown window; UNKNOWN is excluded because benching on
# unclassified noise is worse than surfacing it.
BENCHABLE_FAILURE_KINDS: Final[frozenset[ProviderFailureKind]] = frozenset(
    {
        ProviderFailureKind.RATE_LIMITED,
        ProviderFailureKind.PROVIDER_OVERLOADED,
        ProviderFailureKind.TRANSPORT_TRANSIENT,
        ProviderFailureKind.EMPTY_RESPONSE,
        ProviderFailureKind.MALFORMED_RESPONSE,
    }
)

_DeploymentKey = tuple[str, str, str]
_CandidateDeployment = tuple[str, str] | tuple[str, str, str]


def _deployment_key(
    provider: str,
    model: str,
    upstream: str = "",
) -> _DeploymentKey:
    return (
        (provider or "").strip().lower(),
        (model or "").strip(),
        canonicalize_provider_routing_upstream(upstream),
    )


def _candidate_key(candidate: _CandidateDeployment) -> _DeploymentKey:
    if len(candidate) == 2:
        provider, model = candidate
        return _deployment_key(provider, model)
    provider, model, upstream = candidate
    return _deployment_key(provider, model, upstream)


class ProviderHealthLedger:
    """Cross-turn health and half-open admission for provider deployments.

    Feed it classified failures via :meth:`record_failure` and clear strikes
    via :meth:`record_success`; query it via :meth:`eligible` (routing paths —
    enforces the never-strand exemption) or :meth:`is_benched` (raw state,
    for observability). All methods are thread-safe.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        max_cooldown_s: float = DEFAULT_MAX_COOLDOWN_S,
        telemetry_ttl_s: float = DEFAULT_TELEMETRY_TTL_S,
        observation_window_s: float = DEFAULT_OBSERVATION_WINDOW_S,
        observation_window_size: int = DEFAULT_OBSERVATION_WINDOW_SIZE,
        latency_ewma_alpha: float = DEFAULT_LATENCY_EWMA_ALPHA,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if cooldown_s <= 0:
            raise ValueError("cooldown_s must be positive")
        if max_cooldown_s < cooldown_s:
            raise ValueError("max_cooldown_s must be >= cooldown_s")
        if telemetry_ttl_s <= 0:
            raise ValueError("telemetry_ttl_s must be positive")
        if observation_window_s <= 0:
            raise ValueError("observation_window_s must be positive")
        if observation_window_size < 1:
            raise ValueError("observation_window_size must be >= 1")
        if not 0.0 < latency_ewma_alpha <= 1.0:
            raise ValueError("latency_ewma_alpha must be in (0, 1]")
        self._failure_threshold = failure_threshold
        self._cooldown_s = float(cooldown_s)
        self._max_cooldown_s = float(max_cooldown_s)
        self._telemetry_ttl_s = float(telemetry_ttl_s)
        self._observation_window_s = float(observation_window_s)
        self._observation_window_size = int(observation_window_size)
        self._latency_ewma_alpha = float(latency_ewma_alpha)
        self._clock = clock
        self._lock = threading.Lock()
        self._strikes: dict[_DeploymentKey, int] = {}
        self._benched_until: dict[_DeploymentKey, float] = {}
        self._benched_at: dict[_DeploymentKey, float] = {}
        self._half_open: set[_DeploymentKey] = set()
        # A probe lease is ownership, not a boolean.  An older concurrent
        # request (or a rejected busy attempt) must never be able to release
        # the one request currently proving this deployment healthy.
        self._half_open_inflight: dict[_DeploymentKey, str] = {}
        self._last_observation: dict[_DeploymentKey, float] = {}
        self._last_failure_kind: dict[_DeploymentKey, ProviderFailureKind] = {}
        self._observations: dict[
            _DeploymentKey,
            deque[tuple[float, bool, str]],
        ] = {}
        self._ewma_p95_ms: dict[_DeploymentKey, float] = {}

    def record_failure(
        self,
        provider: str,
        model: str,
        kind: ProviderFailureKind,
        *,
        retry_after_s: float | None = None,
        upstream: str = "",
        latency_ms: float | None = None,
        lease_token: str | None = None,
        now: float | None = None,
    ) -> bool:
        """Record one classified failure; returns whether the deployment is benched.

        Non-benchable kinds (see ``BENCHABLE_FAILURE_KINDS``) neither count a
        strike nor bench. ``RATE_LIMITED`` benches immediately; other
        benchable kinds bench once the strike threshold is reached. The
        cooldown is ``retry_after_s`` when provided (clamped to
        ``max_cooldown_s``), else the default. ``now`` overrides the clock
        reading and must be in the same monotonic domain.
        """
        key = _deployment_key(provider, model, upstream)
        with self._lock:
            ts = self._clock() if now is None else now
            self._expire_locked(key, ts)
            self._record_observation_locked(
                key,
                ts=ts,
                success=False,
                kind=kind,
                latency_ms=latency_ms,
            )
            owns_probe = self._lease_matches_locked(key, lease_token)
            was_benched = key in self._benched_until
            if owns_probe:
                self._half_open_inflight.pop(key, None)
            if kind not in BENCHABLE_FAILURE_KINDS:
                return key in self._benched_until
            strikes = self._strikes.get(key, 0) + 1
            self._strikes[key] = strikes
            immediate = bool(
                kind is ProviderFailureKind.RATE_LIMITED
                or owns_probe
                or was_benched
            )
            if not immediate and strikes < self._failure_threshold:
                return key in self._benched_until
            cooldown = self._cooldown_for(retry_after_s)
            benched_until = ts + cooldown
            # Strikes are consumed by the bench: after the cooldown the
            # deployment starts from a clean slate instead of re-benching on
            # its first post-cooldown failure.
            self._strikes.pop(key, None)
            self._half_open.discard(key)
            if self._benched_until.get(key, float("-inf")) >= benched_until:
                # Even when an earlier Retry-After keeps a later deadline,
                # this failure is the newest recovery barrier. A sibling
                # request that started before it must not clear the circuit.
                self._benched_at[key] = ts
                return True
            self._benched_until[key] = benched_until
            self._benched_at[key] = ts
            log.warning(
                "provider_health.benched",
                provider=key[0],
                model=key[1],
                kind=kind.value,
                cooldown_s=round(cooldown, 3),
                strikes=strikes,
                immediate=immediate,
            )
            return True

    def record_success(
        self,
        provider: str,
        model: str,
        *,
        upstream: str = "",
        latency_ms: float | None = None,
        attempt_started_at: float | None = None,
        lease_token: str | None = None,
        now: float | None = None,
    ) -> None:
        """Record a healthy physical completion and close a half-open probe.

        A completion that started before a newer bench cannot clear that
        bench. This prevents one late sibling success from undoing a fresh
        429/5xx observation produced by another in-flight request.
        """
        key = _deployment_key(provider, model, upstream)
        with self._lock:
            ts = self._clock() if now is None else now
            self._expire_locked(key, ts)
            self._record_observation_locked(
                key,
                ts=ts,
                success=True,
                kind=None,
                latency_ms=latency_ms,
            )
            benched_at = self._benched_at.get(key)
            stale_success = bool(
                benched_at is not None
                and attempt_started_at is not None
                and attempt_started_at <= benched_at
            )
            owns_probe = self._lease_matches_locked(key, lease_token)
            lease_owned_by_other = bool(
                key in self._half_open_inflight and not owns_probe
            )
            if owns_probe:
                self._half_open_inflight.pop(key, None)
            if stale_success or lease_owned_by_other:
                return
            self._strikes.pop(key, None)
            was_benched = bool(
                self._benched_until.pop(key, None) is not None
                or key in self._half_open
            )
            self._benched_at.pop(key, None)
            self._half_open.discard(key)
        if was_benched:
            log.info(
                "provider_health.unbenched",
                provider=key[0],
                model=key[1],
                reason="success",
            )

    def is_benched(
        self,
        provider: str,
        model: str,
        *,
        upstream: str = "",
        now: float | None = None,
    ) -> bool:
        """Raw bench state, without the single-deployment exemption.

        Routing paths should prefer :meth:`eligible`, which knows the
        candidate set and therefore can enforce the never-strand rule.
        """
        key = _deployment_key(provider, model, upstream)
        with self._lock:
            ts = self._clock() if now is None else now
            self._expire_locked(key, ts)
            return key in self._benched_until

    def eligible(
        self,
        provider: str,
        model: str,
        candidate_deployments: Iterable[_CandidateDeployment],
        *,
        upstream: str = "",
        now: float | None = None,
    ) -> bool:
        """Whether routing may use this deployment, given the tier's candidates.

        ``candidate_deployments`` is every (provider, model) pair that could
        serve the need (it may include the queried pair). A benched
        deployment is reported eligible anyway when no alternative candidate
        is unbenched: a bench that strands routing is worse than one more
        failed attempt.
        """
        key = _deployment_key(provider, model, upstream)
        with self._lock:
            ts = self._clock() if now is None else now
            candidates = {_candidate_key(candidate) for candidate in candidate_deployments}
            return self._eligible_locked(key, candidates, ts, emit_log=True)

    def begin_attempt(
        self,
        provider: str,
        model: str,
        *,
        upstream: str = "",
        never_strand_exempt: bool = False,
        now: float | None = None,
    ) -> dict[str, object]:
        """Admit a physical request, taking the sole half-open probe lease.

        Healthy deployments do not need a lease. A benched deployment can be
        probed early only when ranking explicitly marked it as the
        never-strand exemption. Cooldown-expired deployments admit exactly
        one half-open request until success/failure/cancellation is reported.
        """
        key = _deployment_key(provider, model, upstream)
        with self._lock:
            ts = self._clock() if now is None else now
            self._expire_locked(key, ts)
            state = self._state_locked(key)
            if key in self._half_open_inflight:
                return {
                    "allowed": False,
                    "state": state,
                    "reason": "runtime_deployment_half_open_busy",
                    "probe": True,
                    "started_at": ts,
                }
            if state == "benched" and not never_strand_exempt:
                return {
                    "allowed": False,
                    "state": state,
                    "reason": "runtime_deployment_benched",
                    "probe": False,
                    "started_at": ts,
                }
            probe = state in {"benched", "half_open"}
            admission = {
                "allowed": True,
                "state": state,
                "reason": "",
                "probe": probe,
                "started_at": ts,
            }
            if probe:
                lease_token = secrets.token_hex(16)
                self._half_open_inflight[key] = lease_token
                admission["lease_token"] = lease_token
            return admission

    def cancel_attempt(
        self,
        provider: str,
        model: str,
        *,
        upstream: str = "",
        lease_token: str | None = None,
    ) -> None:
        """Release an admission that never crossed the physical boundary."""
        key = _deployment_key(provider, model, upstream)
        with self._lock:
            if self._lease_matches_locked(key, lease_token):
                self._half_open_inflight.pop(key, None)

    def runtime_facts(
        self,
        provider: str,
        model: str,
        *,
        upstream: str = "",
        candidate_deployments: Iterable[_CandidateDeployment] = (),
        now: float | None = None,
    ) -> dict[str, object]:
        """Return a secret-free, TTL-bounded registry overlay snapshot."""
        key = _deployment_key(provider, model, upstream)
        with self._lock:
            ts = self._clock() if now is None else now
            self._expire_locked(key, ts)
            candidates = {_candidate_key(candidate) for candidate in candidate_deployments}
            eligible = self._eligible_locked(key, candidates, ts, emit_log=False)
            state = self._state_locked(key)
            observations = self._recent_observations_locked(key, ts)
            successes = sum(1 for _, success, _ in observations if success)
            attempts = len(observations)
            observed_at = self._last_observation.get(key)
            fresh = observed_at is not None
            remaining = max(0.0, self._benched_until.get(key, ts) - ts)
            raw_unavailable = state == "benched" or key in self._half_open_inflight
            return {
                "schema": "opensquilla.provider-health-runtime-facts/v1",
                "provider": key[0],
                "model": key[1],
                "upstream": key[2],
                "state": state,
                "fresh": fresh,
                "observation_age_s": (
                    round(max(0.0, ts - observed_at), 3)
                    if observed_at is not None
                    else None
                ),
                "benched_remaining_s": round(remaining, 3),
                "half_open_inflight": key in self._half_open_inflight,
                "eligible": eligible,
                "never_strand_exempt": bool(raw_unavailable and eligible),
                "recent_attempts": attempts,
                "recent_successes": successes,
                "recent_failures": attempts - successes,
                "recent_success_rate": (
                    round(successes / attempts, 6) if attempts else None
                ),
                "ewma_p95_ms": (
                    round(self._ewma_p95_ms[key], 3)
                    if key in self._ewma_p95_ms
                    else None
                ),
                "last_failure_kind": (
                    self._last_failure_kind[key].value
                    if key in self._last_failure_kind
                    else ""
                ),
            }

    def _cooldown_for(self, retry_after_s: float | None) -> float:
        if retry_after_s is None:
            return self._cooldown_s
        try:
            retry_after = float(retry_after_s)
        except (TypeError, ValueError):
            return self._cooldown_s
        if not math.isfinite(retry_after):
            return self._cooldown_s
        return min(max(retry_after, 0.0), self._max_cooldown_s)

    def _lease_matches_locked(
        self,
        key: _DeploymentKey,
        lease_token: str | None,
    ) -> bool:
        """Whether ``lease_token`` owns the active half-open probe."""

        return bool(
            lease_token
            and self._half_open_inflight.get(key) == lease_token
        )

    def _state_locked(self, key: _DeploymentKey) -> str:
        if key in self._benched_until:
            return "benched"
        if key in self._half_open:
            return "half_open"
        return "healthy"

    def _eligible_locked(
        self,
        key: _DeploymentKey,
        candidates: set[_DeploymentKey],
        ts: float,
        *,
        emit_log: bool,
    ) -> bool:
        self._expire_locked(key, ts)
        unavailable = key in self._benched_until or key in self._half_open_inflight
        if not unavailable:
            return True
        alternatives = set(candidates)
        alternatives.discard(key)
        for alternative in alternatives:
            self._expire_locked(alternative, ts)
            if (
                alternative not in self._benched_until
                and alternative not in self._half_open_inflight
            ):
                return False
        if emit_log:
            log.info(
                "provider_health.bench_exempted_only_deployment",
                provider=key[0],
                model=key[1],
                candidates=len(alternatives) + 1,
            )
        return True

    def _record_observation_locked(
        self,
        key: _DeploymentKey,
        *,
        ts: float,
        success: bool,
        kind: ProviderFailureKind | None,
        latency_ms: float | None,
    ) -> None:
        self._last_observation[key] = ts
        if kind is not None:
            self._last_failure_kind[key] = kind
        rows = self._observations.get(key)
        if rows is None:
            rows = deque(maxlen=self._observation_window_size)
            self._observations[key] = rows
        rows.append((ts, success, kind.value if kind is not None else ""))
        if latency_ms is None:
            return
        latency = max(0.0, float(latency_ms))
        estimate = self._ewma_p95_ms.get(key)
        if estimate is None:
            self._ewma_p95_ms[key] = latency
            return
        # Stochastic EWMA quantile: high samples move the estimate quickly,
        # low samples decay it slowly, approximating a recent p95 without an
        # unbounded latency buffer.
        weight = 0.95 if latency >= estimate else 0.05
        self._ewma_p95_ms[key] = estimate + (
            self._latency_ewma_alpha * weight * (latency - estimate)
        )

    def _recent_observations_locked(
        self,
        key: _DeploymentKey,
        ts: float,
    ) -> list[tuple[float, bool, str]]:
        rows = self._observations.get(key)
        if rows is None:
            return []
        cutoff = ts - self._observation_window_s
        while rows and rows[0][0] < cutoff:
            rows.popleft()
        if not rows:
            self._observations.pop(key, None)
            return []
        return list(rows)

    def _expire_stale_locked(self, key: _DeploymentKey, ts: float) -> None:
        observed_at = self._last_observation.get(key)
        if observed_at is None or ts - observed_at < self._telemetry_ttl_s:
            return
        self._strikes.pop(key, None)
        self._benched_until.pop(key, None)
        self._benched_at.pop(key, None)
        self._half_open.discard(key)
        self._half_open_inflight.pop(key, None)
        self._last_observation.pop(key, None)
        self._last_failure_kind.pop(key, None)
        self._observations.pop(key, None)
        self._ewma_p95_ms.pop(key, None)

    def _expire_locked(self, key: _DeploymentKey, ts: float) -> None:
        self._expire_stale_locked(key, ts)
        until = self._benched_until.get(key)
        if until is not None and until <= ts:
            del self._benched_until[key]
            self._half_open.add(key)
            log.info(
                "provider_health.unbenched",
                provider=key[0],
                model=key[1],
                reason="cooldown_expired",
            )


_shared_ledger: ProviderHealthLedger | None = None
_shared_ledger_lock = threading.Lock()


def get_provider_health_ledger() -> ProviderHealthLedger:
    """Process-wide shared ledger, constructed lazily with the D13 defaults.

    Deployment health is global, not per-turn, so opt-in consumers (e.g.
    ``_SelectorFallbackProvider(..., health_ledger=...)`` and live dynamic
    ensembles) should share this instance. Static and frozen routing paths do
    not opt in.
    """
    global _shared_ledger
    with _shared_ledger_lock:
        if _shared_ledger is None:
            _shared_ledger = ProviderHealthLedger()
        return _shared_ledger
