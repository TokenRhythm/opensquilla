"""Process-local Retry-After admission; callers own retries and accounting."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import secrets
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

_SCOPE_HMAC_KEY = secrets.token_bytes(32)
MAX_AUTOMATIC_RETRY_WAIT_SECONDS = 900.0
type RetryAfterFailureReason = Literal["rate_limited", "provider_overloaded"]


@dataclass(slots=True)
class RetryAfterDispatchEvidence:
    """One logical stream's monotonic evidence, shared by every physical leaf."""

    cooling_observed: bool = False
    physical_started: bool = False


_dispatch_evidence: ContextVar[RetryAfterDispatchEvidence | None] = ContextVar(
    "provider_retry_after_dispatch_evidence",
    default=None,
)


async def observe_retry_after_dispatch[StreamItem](
    stream: AsyncIterator[StreamItem],
    evidence: RetryAfterDispatchEvidence,
) -> AsyncIterator[StreamItem]:
    """Bind the same object for every pull and every child task it creates.

    Reset before yielding to the consumer; never leave context installed in
    the Agent's caller or carry a previous logical call into its successor.
    """

    try:
        while True:
            token = _dispatch_evidence.set(evidence)
            try:
                event = await anext(stream)
            except StopAsyncIteration:
                return
            finally:
                _dispatch_evidence.reset(token)
            yield event
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()


def mark_retry_after_physical_start() -> None:
    evidence = _dispatch_evidence.get()
    if evidence is not None:
        evidence.physical_started = True


@dataclass(frozen=True, slots=True)
class RetryAfterScope:
    """Opaque identity of a known physical credential/endpoint/model scope."""

    digest: bytes = field(repr=False)


def retry_after_scope(
    *,
    provider_kind: str,
    base_url: str,
    model: str,
    api_key: str,
    org_id: str,
    authoritative: bool,
) -> RetryAfterScope | None:
    """Associate new adapters only when their complete authority is known.

    An unknown or credential-free extension falls back to object-local state.
    Endpoint normalization deliberately does not guess equivalent URL paths.
    Neither key material nor its unsalted hash is retained in the ledger.
    """

    provider_kind = provider_kind.strip().lower()
    base_url = base_url.strip().rstrip("/")
    model = model.strip()
    if not authoritative or not all((provider_kind, base_url, model, api_key)):
        return None
    identity = json.dumps(
        [provider_kind, base_url, model, api_key, org_id.strip()],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return RetryAfterScope(hmac.digest(_SCOPE_HMAC_KEY, identity, hashlib.sha256))


def provider_retry_after_scope(provider: object) -> RetryAfterScope | None:
    """Read the actual adapter identity, never a caller's requested model."""

    from opensquilla.provider.protocol import provider_connection_config

    try:
        connection = provider_connection_config(provider)
        return retry_after_scope(
            provider_kind=connection.provider_kind,
            base_url=connection.base_url,
            model=connection.model,
            api_key=connection.api_key,
            org_id=connection.org_id,
            authoritative=connection.retry_after_scope_known,
        )
    except Exception:  # noqa: BLE001 - optional metadata cannot block an existing provider
        return None


def _positive_hint(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if not math.isnan(parsed) and parsed > 0 else 0.0


def resolve_retry_delay_seconds(
    *,
    local_delay_s: float,
    provider_retry_after_s: float | None,
    maximum_wait_seconds: float = MAX_AUTOMATIC_RETRY_WAIT_SECONDS,
) -> float | None:
    """Preserve normal retry policy: never clamp an upstream hint early."""

    local = max(0.0, float(local_delay_s))
    hint = _positive_hint(provider_retry_after_s)
    if hint > maximum_wait_seconds:
        return None
    return min(max(local, hint), maximum_wait_seconds)


@dataclass(slots=True)
class _CoolingEntry:
    not_before: float
    reason: RetryAfterFailureReason
    # Strong reference prevents id reuse. No hashing/weakref support is needed.
    owner: object | None = field(repr=False)


class RetryAfterDeferredError(RuntimeError):
    """Admission cannot wait inside its existing time budget; no call was sent."""

    def __init__(
        self,
        remaining_seconds: float,
        reason: str,
        failure_reason: RetryAfterFailureReason = "rate_limited",
    ) -> None:
        super().__init__(reason)
        self.remaining_seconds = remaining_seconds
        self.reason = reason
        self.failure_reason = failure_reason

    @property
    def code(self) -> str:
        if self.failure_reason == "provider_overloaded":
            return (
                "provider_overload_retry_after_deadline"
                if self.reason == "provider_retry_after_deadline"
                else "provider_overload_retry_wait_exhausted"
            )
        return (
            "provider_retry_after_deadline"
            if self.reason == "provider_retry_after_deadline"
            else "rate_limit_retry_exhausted"
        )

    @property
    def message(self) -> str:
        return (
            "The provider retry delay exceeds this request deadline."
            if self.reason == "provider_retry_after_deadline"
            else "The provider retry delay exceeds the automatic wait limit."
        )


class RetryAfterWaitTimeoutError(TimeoutError):
    """The actual parent deadline expired while waiting for admission."""

    def __init__(self, deadline_at_monotonic: float) -> None:
        super().__init__("parent deadline expired before provider dispatch")
        self.deadline_at_monotonic = deadline_at_monotonic


class ProviderRetryAfterCooldowns:
    """Thread-safe not-before deadlines, independent of provider success."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, bytes | int], _CoolingEntry] = {}

    @staticmethod
    def _key(provider: object, scope: RetryAfterScope | None) -> tuple[str, bytes | int]:
        return ("scope", scope.digest) if scope is not None else ("object", id(provider))

    def _expire_locked(self, now: float) -> None:
        for key in [key for key, entry in self._entries.items() if entry.not_before <= now]:
            del self._entries[key]

    def record(
        self,
        provider: object,
        retry_after_s: object,
        *,
        scope: RetryAfterScope | None = None,
        reason: RetryAfterFailureReason = "rate_limited",
    ) -> None:
        hint = _positive_hint(retry_after_s)
        with self._lock:
            now = self._clock()
            self._expire_locked(now)
            # RFC parsing already rejects non-finite hints. Custom provider
            # infinity is not a process-lifetime account ban; normal current-
            # call retry policy still refuses it instead of clamping it.
            if not hint or not math.isfinite(hint) or not math.isfinite(now + hint):
                return
            key = self._key(provider, scope)
            existing = self._entries.get(key)
            not_before = max(now + hint, existing.not_before if existing is not None else now)
            if existing is not None and existing.not_before > now + hint:
                reason = existing.reason
            self._entries[key] = _CoolingEntry(
                not_before,
                reason,
                None if scope is not None else provider,
            )

    def remaining(
        self,
        provider: object,
        *,
        scope: RetryAfterScope | None = None,
    ) -> float:
        with self._lock:
            now = self._clock()
            self._expire_locked(now)
            entry = self._entries.get(self._key(provider, scope))
            if entry is None or scope is None and entry.owner is not provider:
                return 0.0
            return max(0.0, entry.not_before - now)

    def reason(
        self, provider: object, *, scope: RetryAfterScope | None = None,
    ) -> RetryAfterFailureReason:
        with self._lock:
            self._expire_locked(self._clock())
            entry = self._entries.get(self._key(provider, scope))
            return entry.reason if entry is not None else "rate_limited"

    async def wait(
        self,
        provider: object,
        *,
        scope: RetryAfterScope | None = None,
        deadline_at_monotonic: float | None,
        maximum_wait_seconds: float = MAX_AUTOMATIC_RETRY_WAIT_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> AsyncIterator[float]:
        """Yield deliberate waits before sleeping; send only after exhaustion.

        Invoke outside the provider idle guard and before usage/dispatch starts.
        A yielded value can use the existing retry_wait activity event. Recheck
        state after sleep because another in-flight call can extend the hint.
        Cancellation and actual parent timeouts propagate without conversion.
        """

        started = self._clock()
        wait_deadline = started + maximum_wait_seconds
        while True:
            now = self._clock()
            if deadline_at_monotonic is not None and now >= deadline_at_monotonic:
                raise RetryAfterWaitTimeoutError(deadline_at_monotonic)
            remaining = self.remaining(provider, scope=scope)
            if remaining <= 0:
                return
            if deadline_at_monotonic is not None and now + remaining >= deadline_at_monotonic:
                raise RetryAfterDeferredError(
                    remaining,
                    "provider_retry_after_deadline",
                    self.reason(provider, scope=scope),
                )
            delay = resolve_retry_delay_seconds(
                local_delay_s=0.0,
                provider_retry_after_s=remaining,
                maximum_wait_seconds=max(0.0, wait_deadline - now),
            )
            if delay is None:
                raise RetryAfterDeferredError(
                    remaining,
                    "provider_retry_after_wait_ceiling",
                    self.reason(provider, scope=scope),
                )
            evidence = _dispatch_evidence.get()
            if evidence is not None:
                evidence.cooling_observed = True
            yield delay
            timeout_scope = asyncio.timeout_at(deadline_at_monotonic)
            try:
                async with timeout_scope:
                    await (sleep or self._sleep)(delay)
            except TimeoutError:
                if timeout_scope.expired() and deadline_at_monotonic is not None:
                    raise RetryAfterWaitTimeoutError(deadline_at_monotonic) from None
                raise


_provider_retry_after_cooldowns = ProviderRetryAfterCooldowns()


def provider_retry_after_cooldowns() -> ProviderRetryAfterCooldowns:
    return _provider_retry_after_cooldowns


def record_provider_retry_after(provider: object, event: object) -> None:
    """Record the hint immediately on arrival, before retries or conversion."""

    code = str(getattr(event, "code", ""))
    if code not in {"429", "503"} or getattr(event, "tool_argument_rejection", None) is not None:
        return
    provider_retry_after_cooldowns().record(
        provider,
        getattr(event, "retry_after_s", None),
        scope=provider_retry_after_scope(provider),
        reason="rate_limited" if code == "429" else "provider_overloaded",
    )
