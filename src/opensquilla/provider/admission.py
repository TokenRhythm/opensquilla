"""Process-local admission control for physical LLM provider requests.

The controller is intentionally independent from routing policy.  Routing
decides *which* deployment should run; admission only bounds how many physical
requests may be in flight across concurrent turns on one event loop.

Capacity is reserved atomically across the global, provider, and deployment
levels from one strict-FIFO queue. A queued narrow deployment therefore never
holds wider capacity while it waits. Strict FIFO can delay an otherwise fitting
follower behind the head waiter, but every wait is bounded by both the caller's
absolute deadline and a configured queue-wait ceiling. A lease is idempotent so
cancellation or exception paths can safely release capacity exactly once.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
import weakref
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


def _normalized_provider(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    return normalized or "unknown"


def deployment_key(provider: str, model: str) -> str:
    """Return the public, non-credential identity used for endpoint limits.

    The first implementation deliberately keys a deployment by configured
    provider/model only. It does not claim to isolate two upstreams serving the
    same model identity; adding a verified upstream discriminator is a later
    health-ledger integration, not something inferred from secret URLs here.
    """

    normalized_provider = _normalized_provider(provider)
    normalized_model = str(model or "").strip().casefold() or "unknown"
    return f"{normalized_provider}/{normalized_model}"


@dataclass(frozen=True)
class ProviderAdmissionSettings:
    """Immutable process-local admission limits.

    ``deployment_weights`` consumes the same number of slots at all three
    levels.  A weight of two therefore halves the effective concurrency of an
    endpoint without introducing another scheduler or priority class.
    """

    global_max_in_flight: int = 24
    provider_default_max_in_flight: int = 8
    deployment_default_max_in_flight: int = 4
    queue_timeout_seconds: float = 5.0
    provider_limits: Mapping[str, int] = field(default_factory=dict)
    deployment_limits: Mapping[str, int] = field(default_factory=dict)
    deployment_weights: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for label, value in (
            ("global_max_in_flight", self.global_max_in_flight),
            ("provider_default_max_in_flight", self.provider_default_max_in_flight),
            ("deployment_default_max_in_flight", self.deployment_default_max_in_flight),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        queue_timeout = self.queue_timeout_seconds
        if (
            isinstance(queue_timeout, bool)
            or not isinstance(queue_timeout, (int, float))
            or not math.isfinite(queue_timeout)
            or queue_timeout <= 0
        ):
            raise ValueError("queue_timeout_seconds must be finite and positive")
        object.__setattr__(self, "queue_timeout_seconds", float(queue_timeout))
        normalized_provider_limits: dict[str, int] = {}
        for key, value in self.provider_limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("provider_limits values must be positive integers")
            normalized_provider_limits[_normalized_provider(key)] = value
        normalized_deployment_limits: dict[str, int] = {}
        for key, value in self.deployment_limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("deployment_limits values must be positive integers")
            normalized_deployment_limits[deployment_key(*_split_deployment_key(key))] = value
        normalized_deployment_weights: dict[str, int] = {}
        for key, value in self.deployment_weights.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("deployment_weights values must be positive integers")
            normalized_deployment_weights[deployment_key(*_split_deployment_key(key))] = value
        object.__setattr__(self, "provider_limits", normalized_provider_limits)
        object.__setattr__(self, "deployment_limits", normalized_deployment_limits)
        object.__setattr__(self, "deployment_weights", normalized_deployment_weights)

    def provider_capacity(self, provider: str) -> int:
        return int(
            self.provider_limits.get(
                _normalized_provider(provider),
                self.provider_default_max_in_flight,
            )
        )

    def deployment_capacity(self, provider: str, model: str) -> int:
        return int(
            self.deployment_limits.get(
                deployment_key(provider, model),
                self.deployment_default_max_in_flight,
            )
        )

    def deployment_weight(self, provider: str, model: str) -> int:
        return int(self.deployment_weights.get(deployment_key(provider, model), 1))

    def fingerprint(self) -> str:
        payload = repr(
            (
                self.global_max_in_flight,
                self.provider_default_max_in_flight,
                self.deployment_default_max_in_flight,
                self.queue_timeout_seconds,
                tuple(sorted(self.provider_limits.items())),
                tuple(sorted(self.deployment_limits.items())),
                tuple(sorted(self.deployment_weights.items())),
            )
        )
        return hashlib.sha256(payload.encode()).hexdigest()


def _split_deployment_key(value: str) -> tuple[str, str]:
    normalized = str(value or "").strip().casefold()
    provider, separator, model = normalized.partition("/")
    if not separator or not provider or not model:
        raise ValueError("deployment admission keys must use '<provider>/<model>'")
    return provider, model


class ProviderAdmissionError(RuntimeError):
    """Base class for a physical request rejected before dispatch."""

    code = "provider_admission_error"

    def __init__(self, *, role: str, provider: str, model: str, wait_ms: int) -> None:
        self.role = str(role or "provider")
        self.provider = _normalized_provider(provider)
        self.deployment = deployment_key(provider, model)
        self.wait_ms = max(0, int(wait_ms))
        super().__init__(f"{self.role} provider admission failed before physical dispatch")


class ProviderAdmissionTimeoutError(ProviderAdmissionError):
    """The request could not acquire capacity before its queue deadline."""

    code = "provider_admission_timeout"


class ProviderAdmissionCapacityError(ProviderAdmissionError):
    """A configured request weight cannot fit into one of its capacities."""

    code = "provider_admission_capacity"


class ProviderAdmissionLease:
    """Idempotent lease spanning global, provider, and deployment capacity."""

    def __init__(
        self,
        *,
        controller: ProviderAdmissionController,
        provider: str,
        deployment: str,
        weight: int,
        wait_ms: int,
    ) -> None:
        self._controller = controller
        self.provider = provider
        self.deployment = deployment
        self.weight = weight
        self.wait_ms = wait_ms
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._controller._release(
            provider=self.provider,
            deployment=self.deployment,
            weight=self.weight,
        )

    async def __aenter__(self) -> ProviderAdmissionLease:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.release()


class ProviderAdmissionLeaseGuard:
    """Retain a lease until every registered physical cleanup proof settles.

    Stream relays may return a typed close-timeout while an owned ``aclose``
    task is still running in the background. Releasing capacity at that point
    would let another turn overlap the same unresolved physical request. The
    guard transfers ownership to every cleanup future registered by the relay
    and releases only after ``finish`` was requested and the tracked set is
    empty.

    ``before_release`` is the composition point for deployment-health feedback:
    a combined runtime records the final health/benchmark observation there,
    before releasing capacity can drain and dispatch the next waiter.
    """

    def __init__(
        self,
        lease: ProviderAdmissionLease,
        *,
        pending_cleanup_tracker: (
            Callable[[asyncio.Future[Any], str], None] | None
        ) = None,
        before_release: Callable[[], None] | None = None,
    ) -> None:
        self._lease = lease
        self._pending_cleanup_tracker = pending_cleanup_tracker
        self._before_release = before_release
        self._tracked_cleanup: set[asyncio.Future[Any]] = set()
        self._finish_requested = False
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    @property
    def pending_cleanup_count(self) -> int:
        return len(self._tracked_cleanup)

    def track_cleanup(
        self,
        future: asyncio.Future[Any],
        phase: str,
    ) -> None:
        """Register cleanup before its first cancellable wait."""

        if future in self._tracked_cleanup:
            return
        if self._released:
            raise RuntimeError(
                "cannot register cleanup after provider admission release"
            )
        self._tracked_cleanup.add(future)

        def _cleanup_done(done: asyncio.Future[Any]) -> None:
            # Raw ``__anext__`` callbacks can synchronously register a deferred
            # ``aclose``. Settle one loop turn later so that handoff is atomic:
            # capacity never opens between the two cleanup owners.
            try:
                asyncio.get_running_loop().call_soon(
                    self._settle_cleanup,
                    done,
                )
            except RuntimeError:
                self._settle_cleanup(done)

        try:
            if self._pending_cleanup_tracker is not None:
                self._pending_cleanup_tracker(future, phase)
        finally:
            # Register our release callback after the caller's physical-cleanup
            # observer. Both observers defer one loop turn; this ordering
            # ensures health/accounting evidence is settled before
            # ``before_release`` can publish feedback and drain the next waiter.
            # The ``finally`` also prevents a faulty observer from leaking the
            # capacity it was asked to track.
            future.add_done_callback(_cleanup_done)

    def _settle_cleanup(self, future: asyncio.Future[Any]) -> None:
        self._tracked_cleanup.discard(future)
        self._release_if_ready()

    def finish(self) -> None:
        """Release now if closed, otherwise transfer ownership to cleanup."""

        self._finish_requested = True
        self._release_if_ready()

    def _release_if_ready(self) -> None:
        if (
            self._released
            or not self._finish_requested
            or self._tracked_cleanup
        ):
            return
        self._released = True
        try:
            if self._before_release is not None:
                self._before_release()
        finally:
            self._lease.release()


@dataclass
class _AdmissionWaiter:
    provider: str
    deployment: str
    weight: int
    future: asyncio.Future[None]
    granted: bool = False


class ProviderAdmissionController:
    """Shared admission controller for one event loop and runtime config."""

    def __init__(self, settings: ProviderAdmissionSettings) -> None:
        self.settings = settings
        self._loop_ref: weakref.ReferenceType[asyncio.AbstractEventLoop] | None = None
        self._global_in_flight = 0
        self._provider_in_flight: dict[str, int] = {}
        self._deployment_in_flight: dict[str, int] = {}
        self._waiters: deque[_AdmissionWaiter] = deque()
        self._active_leases = 0
        self._total_acquired = 0
        self._total_released = 0
        self._total_timeouts = 0
        self._total_cancelled = 0

    @property
    def active_leases(self) -> int:
        return self._active_leases

    @property
    def queued(self) -> int:
        return sum(1 for waiter in self._waiters if not waiter.future.done())

    @property
    def in_flight_units(self) -> int:
        return self._global_in_flight

    def _fits(self, waiter: _AdmissionWaiter) -> bool:
        provider_capacity = self.settings.provider_capacity(waiter.provider)
        provider, _, model = waiter.deployment.partition("/")
        deployment_capacity = self.settings.deployment_capacity(provider, model)
        return bool(
            self._global_in_flight + waiter.weight <= self.settings.global_max_in_flight
            and self._provider_in_flight.get(waiter.provider, 0) + waiter.weight
            <= provider_capacity
            and self._deployment_in_flight.get(waiter.deployment, 0) + waiter.weight
            <= deployment_capacity
        )

    def _bind_or_assert_current_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        owner_loop = self._loop_ref() if self._loop_ref is not None else None
        if self._loop_ref is None:
            self._loop_ref = weakref.ref(loop)
        elif owner_loop is not loop:
            raise RuntimeError("provider admission controller cannot be shared across event loops")
        return loop

    def _reserve(self, waiter: _AdmissionWaiter) -> None:
        self._global_in_flight += waiter.weight
        self._provider_in_flight[waiter.provider] = (
            self._provider_in_flight.get(waiter.provider, 0) + waiter.weight
        )
        self._deployment_in_flight[waiter.deployment] = (
            self._deployment_in_flight.get(waiter.deployment, 0) + waiter.weight
        )
        waiter.granted = True

    def _rollback_reservation(self, waiter: _AdmissionWaiter) -> None:
        if not waiter.granted:
            return
        waiter.granted = False
        self._global_in_flight -= waiter.weight
        self._decrement_counter(
            self._provider_in_flight,
            waiter.provider,
            waiter.weight,
        )
        self._decrement_counter(
            self._deployment_in_flight,
            waiter.deployment,
            waiter.weight,
        )

    @staticmethod
    def _decrement_counter(
        values: dict[str, int],
        key: str,
        weight: int,
    ) -> None:
        remaining = values.get(key, 0) - weight
        if remaining < 0:
            raise RuntimeError("provider admission released without ownership")
        if remaining:
            values[key] = remaining
        else:
            values.pop(key, None)

    def _drain(self) -> None:
        # Strict FIFO avoids permanent starvation of a high-weight or busy
        # deployment waiter. Resources are reserved atomically; queued calls
        # never consume global capacity while waiting on a narrower endpoint.
        while self._waiters:
            waiter = self._waiters[0]
            if waiter.future.cancelled():
                self._waiters.popleft()
                continue
            if not self._fits(waiter):
                return
            self._waiters.popleft()
            self._reserve(waiter)
            if not waiter.future.done():
                waiter.future.set_result(None)

    async def acquire(
        self,
        *,
        provider: str,
        model: str,
        role: str,
        absolute_deadline: float | None = None,
        weight: int | None = None,
    ) -> ProviderAdmissionLease:
        started = time.monotonic()
        provider_key = _normalized_provider(provider)
        endpoint_key = deployment_key(provider, model)
        raw_weight = self.settings.deployment_weight(provider, model) if weight is None else weight
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, int):
            raise ProviderAdmissionCapacityError(
                role=role,
                provider=provider,
                model=model,
                wait_ms=0,
            )
        effective_weight = raw_weight
        if effective_weight <= 0 or any(
            effective_weight > capacity
            for capacity in (
                self.settings.global_max_in_flight,
                self.settings.provider_capacity(provider_key),
                self.settings.deployment_capacity(provider, model),
            )
        ):
            raise ProviderAdmissionCapacityError(
                role=role,
                provider=provider,
                model=model,
                wait_ms=0,
            )
        queue_deadline = started + self.settings.queue_timeout_seconds
        if absolute_deadline is not None:
            queue_deadline = min(queue_deadline, absolute_deadline)
        loop = self._bind_or_assert_current_loop()
        waiter = _AdmissionWaiter(
            provider=provider_key,
            deployment=endpoint_key,
            weight=effective_weight,
            future=loop.create_future(),
        )
        self._waiters.append(waiter)
        self._drain()
        try:
            remaining = queue_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                await waiter.future
        except TimeoutError as exc:
            if waiter.granted:
                self._rollback_reservation(waiter)
            else:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
            if not waiter.future.done():
                waiter.future.cancel()
            self._drain()
            self._total_timeouts += 1
            raise ProviderAdmissionTimeoutError(
                role=role,
                provider=provider,
                model=model,
                wait_ms=int((time.monotonic() - started) * 1000),
            ) from exc
        except asyncio.CancelledError:
            if waiter.granted:
                self._rollback_reservation(waiter)
            else:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
            if not waiter.future.done():
                waiter.future.cancel()
            self._drain()
            self._total_cancelled += 1
            raise
        except BaseException:
            if waiter.granted:
                self._rollback_reservation(waiter)
            else:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
            if not waiter.future.done():
                waiter.future.cancel()
            self._drain()
            raise
        self._active_leases += 1
        self._total_acquired += 1
        return ProviderAdmissionLease(
            controller=self,
            provider=provider_key,
            deployment=endpoint_key,
            weight=effective_weight,
            wait_ms=int((time.monotonic() - started) * 1000),
        )

    def _release(
        self,
        *,
        provider: str,
        deployment: str,
        weight: int,
    ) -> None:
        self._bind_or_assert_current_loop()
        if self._active_leases <= 0:
            raise RuntimeError("provider admission lease released without ownership")
        self._active_leases -= 1
        self._global_in_flight -= weight
        if self._global_in_flight < 0:
            raise RuntimeError("provider admission released without ownership")
        self._decrement_counter(self._provider_in_flight, provider, weight)
        self._decrement_counter(
            self._deployment_in_flight,
            deployment,
            weight,
        )
        self._total_released += 1
        self._drain()

    def snapshot(self) -> dict[str, Any]:
        """Return secret-free counters suitable for tests and metrics."""

        return {
            "settings_fingerprint": self.settings.fingerprint(),
            "global_capacity": self.settings.global_max_in_flight,
            "global_in_flight": self._global_in_flight,
            "global_queued": self.queued,
            "active_leases": self._active_leases,
            "total_acquired": self._total_acquired,
            "total_released": self._total_released,
            "total_timeouts": self._total_timeouts,
            "total_cancelled": self._total_cancelled,
            "provider_in_flight": dict(self._provider_in_flight),
            "deployment_in_flight": dict(self._deployment_in_flight),
        }


_SHARED_CONTROLLERS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    ProviderAdmissionController,
] = weakref.WeakKeyDictionary()


def get_shared_provider_admission_controller(
    settings: ProviderAdmissionSettings,
) -> ProviderAdmissionController:
    """Return the single controller shared by turns on the current event loop.

    Gateway configuration is immutable during a process generation.  If a
    caller nevertheless supplies different settings while requests are live,
    reuse the existing controller rather than creating an independent capacity
    island that could bypass the global cap.  Once idle, the next call may
    safely replace it with the new settings.
    """

    loop = asyncio.get_running_loop()
    controller = _SHARED_CONTROLLERS.get(loop)
    if controller is None:
        controller = ProviderAdmissionController(settings)
        _SHARED_CONTROLLERS[loop] = controller
        return controller
    if controller.settings.fingerprint() == settings.fingerprint():
        return controller
    if controller.active_leases == 0 and controller.queued == 0 and controller.in_flight_units == 0:
        controller = ProviderAdmissionController(settings)
        _SHARED_CONTROLLERS[loop] = controller
    return controller


def reset_shared_provider_admission_controllers_for_tests() -> None:
    """Clear idle loop-local controllers; intended only for deterministic tests."""

    busy = [
        controller
        for controller in _SHARED_CONTROLLERS.values()
        if (controller.active_leases or controller.queued or controller.in_flight_units)
    ]
    if busy:
        raise RuntimeError("cannot reset provider admission while requests are active")
    _SHARED_CONTROLLERS.clear()


def provider_admission_settings_from_config(value: object) -> ProviderAdmissionSettings:
    """Detach validated settings from the gateway model without importing it."""

    return ProviderAdmissionSettings(
        global_max_in_flight=getattr(value, "global_max_in_flight", 24),
        provider_default_max_in_flight=getattr(
            value,
            "provider_default_max_in_flight",
            8,
        ),
        deployment_default_max_in_flight=getattr(
            value,
            "deployment_default_max_in_flight",
            4,
        ),
        queue_timeout_seconds=getattr(value, "queue_timeout_seconds", 5.0),
        provider_limits=dict(getattr(value, "provider_limits", {}) or {}),
        deployment_limits=dict(getattr(value, "deployment_limits", {}) or {}),
        deployment_weights=dict(getattr(value, "deployment_weights", {}) or {}),
    )
