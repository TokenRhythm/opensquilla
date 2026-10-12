"""Provider deployment health ledger: temporary benching on classified failures.

Complements :mod:`opensquilla.engine.fallback`: the fallback policy decides
*retry-vs-surface* for one in-flight call, while this ledger answers a
different question — *is this (provider, model) deployment temporarily
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

import threading
import time
from collections.abc import Callable, Iterable
from typing import Final

import structlog

from opensquilla.provider.failures import ProviderFailureKind

log = structlog.get_logger(__name__)

DEFAULT_FAILURE_THRESHOLD: Final[int] = 3
DEFAULT_COOLDOWN_S: Final[float] = 30.0
# Defensive ceiling for Retry-After-driven cooldowns: a broken or hostile
# header must not park a deployment for hours. Generous enough that any
# realistic provider hint is honored verbatim.
DEFAULT_MAX_COOLDOWN_S: Final[float] = 900.0

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

_DeploymentKey = tuple[str, str]


def _deployment_key(provider: str, model: str) -> _DeploymentKey:
    return ((provider or "").strip().lower(), (model or "").strip())


class ProviderHealthLedger:
    """Strike counter + cooldown bench for (provider, model) deployments.

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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if cooldown_s <= 0:
            raise ValueError("cooldown_s must be positive")
        if max_cooldown_s < cooldown_s:
            raise ValueError("max_cooldown_s must be >= cooldown_s")
        self._failure_threshold = failure_threshold
        self._cooldown_s = float(cooldown_s)
        self._max_cooldown_s = float(max_cooldown_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._strikes: dict[_DeploymentKey, int] = {}
        self._benched_until: dict[_DeploymentKey, float] = {}

    def record_failure(
        self,
        provider: str,
        model: str,
        kind: ProviderFailureKind,
        *,
        retry_after_s: float | None = None,
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
        key = _deployment_key(provider, model)
        with self._lock:
            ts = self._clock() if now is None else now
            self._expire_locked(key, ts)
            if kind not in BENCHABLE_FAILURE_KINDS:
                return key in self._benched_until
            strikes = self._strikes.get(key, 0) + 1
            self._strikes[key] = strikes
            immediate = kind is ProviderFailureKind.RATE_LIMITED
            if not immediate and strikes < self._failure_threshold:
                return key in self._benched_until
            cooldown = self._cooldown_for(retry_after_s)
            benched_until = ts + cooldown
            # Strikes are consumed by the bench: after the cooldown the
            # deployment starts from a clean slate instead of re-benching on
            # its first post-cooldown failure.
            self._strikes.pop(key, None)
            if self._benched_until.get(key, float("-inf")) >= benched_until:
                return True
            self._benched_until[key] = benched_until
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

    def record_success(self, provider: str, model: str) -> None:
        """A good call clears the strike count (and any active bench)."""
        key = _deployment_key(provider, model)
        with self._lock:
            self._strikes.pop(key, None)
            was_benched = self._benched_until.pop(key, None) is not None
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
        now: float | None = None,
    ) -> bool:
        """Raw bench state, without the single-deployment exemption.

        Routing paths should prefer :meth:`eligible`, which knows the
        candidate set and therefore can enforce the never-strand rule.
        """
        key = _deployment_key(provider, model)
        with self._lock:
            ts = self._clock() if now is None else now
            self._expire_locked(key, ts)
            return key in self._benched_until

    def eligible(
        self,
        provider: str,
        model: str,
        candidate_deployments: Iterable[tuple[str, str]],
        *,
        now: float | None = None,
    ) -> bool:
        """Whether routing may use this deployment, given the tier's candidates.

        ``candidate_deployments`` is every (provider, model) pair that could
        serve the need (it may include the queried pair). A benched
        deployment is reported eligible anyway when no alternative candidate
        is unbenched: a bench that strands routing is worse than one more
        failed attempt.
        """
        key = _deployment_key(provider, model)
        with self._lock:
            ts = self._clock() if now is None else now
            self._expire_locked(key, ts)
            if key not in self._benched_until:
                return True
            alternatives = {_deployment_key(p, m) for p, m in candidate_deployments}
            alternatives.discard(key)
            for alt in alternatives:
                self._expire_locked(alt, ts)
                if alt not in self._benched_until:
                    return False
            log.info(
                "provider_health.bench_exempted_only_deployment",
                provider=key[0],
                model=key[1],
                candidates=len(alternatives) + 1,
            )
            return True

    def _cooldown_for(self, retry_after_s: float | None) -> float:
        if retry_after_s is None:
            return self._cooldown_s
        return min(max(float(retry_after_s), 0.0), self._max_cooldown_s)

    def _expire_locked(self, key: _DeploymentKey, ts: float) -> None:
        until = self._benched_until.get(key)
        if until is not None and until <= ts:
            del self._benched_until[key]
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
    ``_SelectorFallbackProvider(..., health_ledger=...)``) should share this
    instance. Nothing on the default path calls it.
    """
    global _shared_ledger
    with _shared_ledger_lock:
        if _shared_ledger is None:
            _shared_ledger = ProviderHealthLedger()
        return _shared_ledger
