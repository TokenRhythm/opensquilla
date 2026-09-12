"""In-process linearization for telemetry consent checkpoints and changes."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast

from opensquilla.telemetry.consent import (
    ConsentCheckpoint,
    ScopeConsentState,
    TelemetryScope,
    resolve_scope_consent,
)

type ConsentStateProvider = Callable[
    [TelemetryScope], ScopeConsentState | Awaitable[ScopeConsentState]
]


@dataclass(frozen=True)
class ConsentPermit:
    """Evidence valid only while its coordinator context remains entered."""

    scope: TelemetryScope
    checkpoint: ConsentCheckpoint
    notice_version: str
    revision: int


class ScopeConsentCoordinator:
    """Serialize enqueue/send checkpoints with persisted consent transitions.

    A caller must keep the yielded context entered across the irreversible
    boundary: queue commit for ``ENQUEUE`` and request start for ``SEND``.
    Consent mutation code must keep :meth:`transition` entered from its first
    fail-closed state change through scope cleanup.
    """

    def __init__(self, consent_state: ConsentStateProvider) -> None:
        self._consent_state = consent_state
        self._locks = {scope: asyncio.Lock() for scope in TelemetryScope}
        self._revisions = {scope: 0 for scope in TelemetryScope}

    @asynccontextmanager
    async def authorized(
        self,
        scope: TelemetryScope | str,
        *,
        checkpoint: ConsentCheckpoint | str,
        notice_version: str,
    ) -> AsyncIterator[ConsentPermit | None]:
        """Yield a permit while the scope transition lock remains held."""

        normalized_scope = TelemetryScope(scope)
        normalized_checkpoint = ConsentCheckpoint(checkpoint)
        async with self._locks[normalized_scope]:
            state = await self._read_state(normalized_scope)
            if (
                state is None
                or not state.allowed_at(normalized_checkpoint)
                or state.notice_version != notice_version
            ):
                yield None
                return
            yield ConsentPermit(
                scope=normalized_scope,
                checkpoint=normalized_checkpoint,
                notice_version=notice_version,
                revision=self._revisions[normalized_scope],
            )

    @asynccontextmanager
    async def transition(
        self,
        scope: TelemetryScope | str,
    ) -> AsyncIterator[int]:
        """Linearize one persisted consent change and its local cleanup."""

        normalized_scope = TelemetryScope(scope)
        async with self._locks[normalized_scope]:
            self._revisions[normalized_scope] += 1
            yield self._revisions[normalized_scope]

    def revision(self, scope: TelemetryScope | str) -> int:
        return self._revisions[TelemetryScope(scope)]

    async def _read_state(self, scope: TelemetryScope) -> ScopeConsentState | None:
        try:
            value = self._consent_state(scope)
            if inspect.isawaitable(value):
                value = await cast(Awaitable[ScopeConsentState], value)
        except Exception:
            return None
        if not isinstance(value, ScopeConsentState) or value.scope is not scope:
            return None
        return value


_CONFIG_COORDINATORS: dict[str, ScopeConsentCoordinator] = {}
_CONFIG_COORDINATOR_OWNERS: dict[str, object] = {}


def scope_consent_coordinator_for(
    config: object,
    *,
    state_provider: ConsentStateProvider | None = None,
) -> ScopeConsentCoordinator:
    """Return the process-wide coordinator for one live Gateway config."""

    # Gateway RPC contexts are request-scoped, but all of them receive the
    # same live config object, which config mutations update in place.  Object
    # identity avoids accidentally binding a replacement/test config at the
    # same filesystem path to a stale provider closure.
    key = f"config:{id(config)}"
    coordinator = _CONFIG_COORDINATORS.get(key)
    if coordinator is None:
        provider = state_provider or (lambda scope: resolve_scope_consent(scope, config=config))
        coordinator = ScopeConsentCoordinator(provider)
        _CONFIG_COORDINATORS[key] = coordinator
        # Retaining the owner is intentional: the registry already retains its
        # provider closure for the process lifetime, and this also prevents a
        # later object from reusing the same numeric id with the old coordinator.
        _CONFIG_COORDINATOR_OWNERS[key] = config
    elif _CONFIG_COORDINATOR_OWNERS.get(key) is not config:
        raise RuntimeError("telemetry coordinator owner identity collision")
    elif state_provider is not None:
        raise ValueError("the live config already has a telemetry consent coordinator")
    return coordinator


__all__ = [
    "ConsentPermit",
    "ConsentStateProvider",
    "ScopeConsentCoordinator",
    "scope_consent_coordinator_for",
]
