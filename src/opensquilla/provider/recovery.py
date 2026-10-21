"""Shared recovery for one bound provider, independent of Agent orchestration.

The caller owns content commitment, usage and semantic recovery. This module
owns transient retry decisions and deliberate waits; it never selects models.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

import httpx

from opensquilla.provider.failures import (
    CONNECTION_FAILED_CODE,
    ProviderFailureKind,
    classify_provider_error,
    is_connection_failure,
    retry_after_from_headers,
    structured_provider_error_code,
)
from opensquilla.provider.retry_after import (
    RetryAfterDeferredError,
    RetryAfterWaitTimeoutError,
    mark_retry_after_physical_start,
    provider_retry_after_cooldowns,
    provider_retry_after_scope,
    record_provider_retry_after,
    resolve_retry_delay_seconds,
)
from opensquilla.provider.types import ErrorEvent, ProviderActivityEvent

type RecoveryReason = Literal["rate_limited", "provider_overloaded", "transport_transient"]


async def sleep_before_retry(delay_s: float) -> None:
    """A timer waking early must not violate a provider's admission floor."""
    loop = asyncio.get_running_loop()
    retry_at = loop.time() + delay_s
    remaining = delay_s
    while True:
        await asyncio.sleep(remaining)
        remaining = retry_at - loop.time()
        if remaining <= 0:
            return


def backoff_sleep(attempt: int, base_ms: int = 1000, max_ms: int = 30_000,
                  _fake: bool = False) -> float:
    """Calculate ordinary transient-failure backoff (does not sleep)."""
    jitter = random.randint(0, base_ms // 2)  # noqa: S311
    delay = float(min(base_ms * 2**attempt + jitter, max_ms) / 1000)
    if not _fake:
        import structlog
        structlog.get_logger().info("backoff_sleep", delay_s=round(delay, 2), attempt=attempt)
    return delay


@dataclass(frozen=True, slots=True)
class RetryPlan:
    delay_s: float
    attempt: int
    limit: int
    reason: RecoveryReason


def recovery_delay_seconds(
    *, kind: ProviderFailureKind, attempt: int, provider_retry_after_s: float | None,
    base_ms: int = 1000, max_ms: int = 30_000,
    connection_failed: bool = False,
    deadline_at_monotonic: float | None = None,
    now: float | None = None,
) -> float | None:
    """Admit a wait before publishing it, using the existing absolute deadline.

    No-hint RPM waits use 10/20/40 seconds plus ordinary bounded jitter. A
    trusted server hint remains authoritative and uses ordinary local backoff.
    Explicit zero-backoff configurations stay useful for controlled callers.
    """
    if connection_failed:
        local = min(60.0, 5.0 * 2 ** min(attempt, 4))
    elif kind is ProviderFailureKind.RATE_LIMITED and not provider_retry_after_s:
        if base_ms == 0 or max_ms == 0:
            local = 0.0
        else:
            local = backoff_sleep(attempt, max(10_000, base_ms), max(60_000, max_ms), True)
    else:
        local = backoff_sleep(attempt, base_ms, max_ms, True)
    delay = resolve_retry_delay_seconds(
        local_delay_s=local, provider_retry_after_s=provider_retry_after_s,
    )
    if delay is None:
        return None
    clock = asyncio.get_running_loop().time() if now is None else now
    if deadline_at_monotonic is not None and clock + delay >= deadline_at_monotonic:
        return None
    return delay


def transient_failure_kind(provider: Any, event: ErrorEvent) -> ProviderFailureKind:
    # Legacy code is still classification evidence, never display evidence.
    code = str(getattr(event, "code", "") or "")
    status = getattr(event, "http_status", None)
    if status is None and len(code) == 3 and code.isascii() and code.isdigit():
        status = int(code)
    return classify_provider_error(
        getattr(provider, "provider_name", ""), status,
        getattr(event, "provider_code", "") or code, str(getattr(event, "message", "") or ""),
    )


async def retry_provider_stream(
    stream_factory: Callable[[], AsyncIterator[Any]], *, provider: Any, model: str,
    config: Any, max_retries: int = 3, base_ms: int = 1000, max_ms: int = 30_000,
    content_started: Callable[[], bool] = lambda: False,
    reset_attempt: Callable[[], None] | None = None,
    retry_transient: bool = True,
    sleep: Callable[[float], Awaitable[None]] = sleep_before_retry,
) -> AsyncIterator[Any]:
    """Retry one physical deployment without replaying committed output.

    Adapters keep protocol compatibility resends and their physical accounting.
    This loop consumes only their final error, and marks exhausted errors so an
    outer coordinator cannot multiply the retry budget. Each factory invocation
    must own its accounting scope and close its underlying transport.
    """
    retries = connections = attempts = 0
    activity_id = uuid.uuid4().hex
    physical_limit = max(0, int(getattr(config, "physical_attempt_limit", 0) or 0))
    deadline = getattr(config, "turn_deadline_at_monotonic", None)

    async def attempt_events() -> AsyncIterator[Any]:
        stream = None
        try:
            stream = stream_factory()
            async for event in stream:
                yield event
        except httpx.HTTPStatusError as exc:
            # Ordinary adapters already normalize responses. Typed extension
            # failures still preserve exact response status without raw prose.
            try:
                payload = exc.response.content
            except httpx.ResponseNotRead:
                payload = b""
            yield ErrorEvent(
                message="Provider HTTP request failed", code=str(exc.response.status_code),
                http_status=exc.response.status_code,
                provider_code=structured_provider_error_code(payload),
                retry_after_s=retry_after_from_headers(
                    exc.response.status_code, exc.response.headers,
                ),
            )
        except httpx.RequestError as exc:
            connection = is_connection_failure(exc)
            yield ErrorEvent(
                message="Provider connection failed" if connection else "Provider request error",
                code=CONNECTION_FAILED_CODE if connection else "request_error",
            )
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()

    while True:
        try:
            async for delay in provider_retry_after_cooldowns().wait(
                provider, scope=provider_retry_after_scope(provider),
                deadline_at_monotonic=deadline, sleep=sleep,
            ):
                yield ProviderActivityEvent(
                    activity_id=activity_id, model=model, phase="retry_wait",
                    reason=provider_retry_after_cooldowns().reason(
                        provider, scope=provider_retry_after_scope(provider)),
                    retry_attempt=retries, retry_limit=max_retries,
                    retry_after_ms=math.ceil(delay * 1000),
                    started_at=time.time_ns() // 1_000_000,
                )
        except RetryAfterDeferredError as exc:
            yield ErrorEvent(
                message=exc.message, code=exc.code, retry_after_s=exc.remaining_seconds,
            )
            return
        attempts += 1
        if reset_attempt is not None:
            reset_attempt()
        if deadline is not None and asyncio.get_running_loop().time() >= deadline:
            raise RetryAfterWaitTimeoutError(deadline)
        if not getattr(provider, "owns_retry_after_admission", False):
            mark_retry_after_physical_start()
        stream = attempt_events()
        error: ErrorEvent | None = None
        try:
            async for event in stream:
                if not isinstance(event, ErrorEvent):
                    yield event
                    continue
                record_provider_retry_after(provider, event)
                kind = transient_failure_kind(provider, event)
                connection = event.code == CONNECTION_FAILED_CODE
                retryable = connection or kind is ProviderFailureKind.RATE_LIMITED or (
                    retry_transient and kind in {
                        ProviderFailureKind.PROVIDER_OVERLOADED,
                        ProviderFailureKind.TRANSPORT_TRANSIENT,
                    }
                )
                if (not retryable or event.tool_argument_rejection is not None
                        or getattr(event, "recovery_exhausted", False)
                        or content_started()
                        or getattr(provider, "retry_failed_call_safe", True) is False):
                    yield event
                    return
                # A caller-owned physical allowance delegates the decision to
                # its existing owner (notably Agent's one-attempt admission).
                # This is not evidence that the owner's retry budget ran out.
                if physical_limit and attempts >= physical_limit:
                    yield event
                    return
                if not connection and retries >= max_retries:
                    yield replace(event, recovery_exhausted=True, code=("rate_limit_retry_exhausted"
                        if kind is ProviderFailureKind.RATE_LIMITED else event.code))
                    return
                resolved_delay = recovery_delay_seconds(
                    kind=kind, attempt=connections if connection else retries,
                    provider_retry_after_s=event.retry_after_s,
                    connection_failed=connection, base_ms=base_ms, max_ms=max_ms,
                    deadline_at_monotonic=deadline,
                )
                if resolved_delay is None:
                    yield replace(event, recovery_exhausted=True, code=(
                        "provider_retry_after_deadline"
                        if kind is ProviderFailureKind.RATE_LIMITED
                        else "provider_overload_retry_after_deadline"
                        if kind is ProviderFailureKind.PROVIDER_OVERLOADED
                        else event.code))
                    return
                if connection:
                    connections += 1
                    plan = RetryPlan(resolved_delay, connections, 0, "transport_transient")
                else:
                    retries += 1
                    connections = 0
                    reason: RecoveryReason = (
                        "rate_limited" if kind is ProviderFailureKind.RATE_LIMITED
                        else "provider_overloaded"
                        if kind is ProviderFailureKind.PROVIDER_OVERLOADED
                        else "transport_transient"
                    )
                    plan = RetryPlan(resolved_delay, retries, max_retries, reason)
                error = event
                break
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
        if error is None:
            return
        yield ProviderActivityEvent(
            activity_id=activity_id, model=model, phase="retry_wait", reason=plan.reason,
            retry_attempt=plan.attempt, retry_limit=plan.limit,
            retry_after_ms=math.ceil(plan.delay_s * 1000),
            started_at=time.time_ns() // 1_000_000,
            http_status=getattr(error, "http_status", None),
        )
        async with asyncio.timeout_at(deadline):
            await sleep(plan.delay_s)
        yield ProviderActivityEvent(
            activity_id=activity_id, model=model, phase="retrying", reason=plan.reason,
            retry_attempt=plan.attempt, retry_limit=plan.limit,
            started_at=time.time_ns() // 1_000_000,
        )
