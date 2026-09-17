"""Opt-in, session-scoped provider prompt-cache keepalive service."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

import structlog

from opensquilla.engine.prompt_cache_keepalive import PromptCacheKeepaliveCandidate
from opensquilla.engine.usage_accounting import (
    account_provider_stream,
    bind_usage_accounting_scope,
    provider_accounts_physical_usage,
)
from opensquilla.gateway.usage_ledger_runtime import build_session_usage_scope
from opensquilla.provider import Message, derive_provider_request_correlation
from opensquilla.session.keys import canonicalize_session_key

log = structlog.get_logger(__name__)

DEFAULT_TTL_SECONDS = 300
MIN_TTL_SECONDS = 300
MAX_TTL_SECONDS = 86_400
DEFAULT_IDLE_TIMEOUT_SECONDS = 3_600
MIN_IDLE_TIMEOUT_SECONDS = 300
MAX_IDLE_TIMEOUT_SECONDS = 86_400
_PROBE_RATIO = 0.8
_MAX_SNAPSHOT_CHARS = 2_000_000
_PROBE_MESSAGE = "OpenSquilla prompt cache keepalive probe."


@dataclass(slots=True)
class _Lease:
    ttl_seconds: int
    idle_timeout_seconds: int
    generation: int = 0
    enabled: bool = True
    state: str = "waiting"
    reason: str | None = "waiting_for_stable_prefix"
    candidate: PromptCacheKeepaliveCandidate | None = None
    timer_task: asyncio.Task[None] | None = None
    next_probe_at_ms: int | None = None
    last_probe_at_ms: int | None = None
    last_cache_hit_tokens: int = 0
    idle_deadline_monotonic: float | None = None
    idle_expires_at_ms: int | None = None


class PromptCacheKeepaliveService:
    """Keep a proven prompt prefix warm without creating an Agent turn.

    Leases and snapshots are process memory only. Gateway restart therefore
    restores the safe default (off) and requires an explicit user action.
    """

    def __init__(
        self,
        *,
        task_runtime: Any,
        session_manager: Any,
        usage_event_sink: Any | None,
    ) -> None:
        self._task_runtime = task_runtime
        self._session_manager = session_manager
        self._usage_event_sink = usage_event_sink
        self._leases: dict[str, _Lease] = {}
        self._closed = False

    async def set_enabled(
        self,
        session_key: str,
        *,
        enabled: bool,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        key = canonicalize_session_key(session_key)
        previous = self._leases.get(key)
        if previous is not None:
            previous.generation += 1
            self._cancel_timer(previous)
        await self._task_runtime.cancel_auxiliary(key)
        if not enabled:
            self._leases.pop(key, None)
            return self.status(key)

        self._leases[key] = _Lease(
            ttl_seconds=ttl_seconds,
            idle_timeout_seconds=idle_timeout_seconds,
        )
        return self.status(key)

    def is_enabled(self, session_key: str) -> bool:
        lease = self._leases.get(canonicalize_session_key(session_key))
        return bool(lease is not None and lease.enabled)

    def record_candidate(self, candidate: PromptCacheKeepaliveCandidate) -> None:
        """Capture a successful real-turn prefix only for an armed lease."""

        if self._closed:
            return
        key = canonicalize_session_key(candidate.session_key)
        lease = self._leases.get(key)
        if lease is None or not lease.enabled:
            return
        safe_candidate, reason = self._safe_copy(candidate)
        lease.generation += 1
        self._cancel_timer(lease)
        if safe_candidate is None:
            lease.candidate = None
            lease.state = "waiting"
            lease.reason = reason
            lease.next_probe_at_ms = None
            lease.idle_deadline_monotonic = None
            lease.idle_expires_at_ms = None
            return
        lease.candidate = safe_candidate
        lease.state = "scheduled"
        lease.reason = None
        lease.idle_deadline_monotonic = time.monotonic() + lease.idle_timeout_seconds
        lease.idle_expires_at_ms = int(
            (time.time() + lease.idle_timeout_seconds) * 1000
        )
        self._schedule(key, lease, delay=lease.ttl_seconds * _PROBE_RATIO)

    def refresh_required(self, session_key: str, reason: str = "session_history_changed") -> None:
        """Keep the opt-in setting but discard a stale provider prefix."""

        key = canonicalize_session_key(session_key)
        lease = self._leases.get(key)
        if lease is None or not lease.enabled:
            return
        lease.generation += 1
        self._cancel_timer(lease)
        lease.candidate = None
        lease.state = "waiting"
        lease.reason = reason
        lease.idle_deadline_monotonic = None
        lease.idle_expires_at_ms = None

    def status(self, session_key: str) -> dict[str, Any]:
        key = canonicalize_session_key(session_key)
        lease = self._leases.get(key)
        if lease is None:
            return {
                "enabled": False,
                "ttlSeconds": DEFAULT_TTL_SECONDS,
                "intervalSeconds": int(DEFAULT_TTL_SECONDS * _PROBE_RATIO),
                "idleTimeoutSeconds": DEFAULT_IDLE_TIMEOUT_SECONDS,
                "idleExpiresAt": None,
                "state": "off",
                "reason": None,
                "hasSnapshot": False,
                "nextProbeAt": None,
                "lastProbeAt": None,
                "lastCacheHitTokens": 0,
                "provider": None,
                "model": None,
            }
        candidate = lease.candidate
        return {
            "enabled": lease.enabled,
            "ttlSeconds": lease.ttl_seconds,
            "intervalSeconds": int(lease.ttl_seconds * _PROBE_RATIO),
            "idleTimeoutSeconds": lease.idle_timeout_seconds,
            "idleExpiresAt": lease.idle_expires_at_ms,
            "state": lease.state,
            "reason": lease.reason,
            "hasSnapshot": candidate is not None,
            "nextProbeAt": lease.next_probe_at_ms,
            "lastProbeAt": lease.last_probe_at_ms,
            "lastCacheHitTokens": lease.last_cache_hit_tokens,
            "provider": candidate.provider_id if candidate is not None else None,
            "model": candidate.model if candidate is not None else None,
        }

    async def invalidate(self, session_key: str) -> None:
        """Drop an armed lease when the underlying session identity changes."""

        await self.set_enabled(session_key, enabled=False)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks: list[asyncio.Task[None]] = []
        keys = tuple(self._leases)
        for lease in self._leases.values():
            lease.generation += 1
            if lease.timer_task is not None and not lease.timer_task.done():
                lease.timer_task.cancel()
                tasks.append(lease.timer_task)
        for key in keys:
            await self._task_runtime.cancel_auxiliary(key)
        self._leases.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _cancel_timer(lease: _Lease) -> None:
        task = lease.timer_task
        lease.timer_task = None
        lease.next_probe_at_ms = None
        current = asyncio.current_task()
        if task is not None and task is not current and not task.done():
            task.cancel()

    @staticmethod
    def _safe_copy(
        candidate: PromptCacheKeepaliveCandidate,
    ) -> tuple[PromptCacheKeepaliveCandidate | None, str | None]:
        if candidate.config.thinking:
            return None, "thinking_requests_are_not_supported"
        if candidate.config.output_json_schema is not None:
            return None, "structured_output_requests_are_not_supported"
        if any(
            getattr(block, "type", "") in {"image", "document", "thinking"}
            for message in candidate.messages
            if isinstance(message.content, list)
            for block in message.content
        ):
            return None, "multimodal_or_thinking_history_is_not_supported"
        try:
            messages = tuple(message.model_copy(deep=True) for message in candidate.messages)
            tools = tuple(
                tool.model_copy(deep=True) if hasattr(tool, "model_copy") else tool
                for tool in candidate.tools
            )
            config = candidate.config.model_copy(deep=True)
            snapshot_chars = sum(len(message.model_dump_json()) for message in messages)
            snapshot_chars += sum(
                len(tool.model_dump_json())
                if hasattr(tool, "model_dump_json")
                else len(str(tool))
                for tool in tools
            )
        except Exception:
            return None, "snapshot_copy_failed"
        if snapshot_chars > _MAX_SNAPSHOT_CHARS:
            return None, "stable_prefix_too_large"
        return (
            PromptCacheKeepaliveCandidate(
                session_key=candidate.session_key,
                provider=candidate.provider,
                provider_id=candidate.provider_id,
                model=candidate.model,
                messages=messages,
                tools=tools,
                config=config,
            ),
            None,
        )

    def _schedule(self, key: str, lease: _Lease, *, delay: float) -> None:
        if self._closed or not lease.enabled or lease.candidate is None:
            return
        generation = lease.generation
        delay = max(0.0, float(delay))
        wake_for_idle_expiry = False
        if lease.idle_deadline_monotonic is not None:
            remaining = max(0.0, lease.idle_deadline_monotonic - time.monotonic())
            if delay >= remaining:
                delay = remaining
                wake_for_idle_expiry = True
        lease.next_probe_at_ms = (
            None if wake_for_idle_expiry else int((time.time() + delay) * 1000)
        )
        lease.timer_task = asyncio.create_task(
            self._run_after(key, generation, delay, wake_for_idle_expiry),
            name=f"prompt-cache-keepalive-{key}",
        )

    async def _run_after(
        self,
        key: str,
        generation: int,
        delay: float,
        wake_for_idle_expiry: bool = False,
    ) -> None:
        try:
            await asyncio.sleep(delay)
            lease = self._leases.get(key)
            if (
                lease is None
                or not lease.enabled
                or lease.generation != generation
                or lease.candidate is None
            ):
                return
            if wake_for_idle_expiry or (
                lease.idle_deadline_monotonic is not None
                and time.monotonic() >= lease.idle_deadline_monotonic
            ):
                self._pause_for_idle_timeout(key, generation)
                return
            lease.state = "probing"
            lease.next_probe_at_ms = None
            probe_lease = lease

            async def operation() -> None:
                await self._probe(key, probe_lease, generation)

            ran = await self._task_runtime.run_auxiliary_if_idle(key, operation)
            lease = self._leases.get(key)
            if lease is None or lease.generation != generation or not lease.enabled:
                return
            if not ran:
                lease.state = "scheduled"
                lease.reason = "session_busy"
                self._schedule(
                    key,
                    lease,
                    delay=min(30.0, max(5.0, lease.ttl_seconds * 0.1)),
                )
        except asyncio.CancelledError:
            lease = self._leases.get(key)
            if lease is not None and lease.generation == generation and lease.enabled:
                lease.state = "waiting"
                lease.reason = "preempted_by_user_turn"
                lease.next_probe_at_ms = None
            raise
        except Exception as exc:  # noqa: BLE001 - auxiliary work is fail-closed
            self._stop(key, generation, "probe_failed")
            log.info(
                "prompt_cache_keepalive.probe_failed",
                session_key=key,
                error_class=type(exc).__name__,
            )

    async def _probe(self, key: str, lease: _Lease, generation: int) -> None:
        candidate = lease.candidate
        if candidate is None:
            return
        correlation = derive_provider_request_correlation(
            candidate.config.provider_request_correlation,
            execution_id=uuid.uuid4().hex,
            call_kind="prompt_cache_keepalive",
        )
        config = candidate.config.model_copy(
            update={
                "max_tokens": 1,
                "timeout": min(30.0, max(1.0, float(candidate.config.timeout))),
                "physical_attempt_limit": 1,
                "provider_request_correlation": correlation,
                "active_user_message_index": len(candidate.messages),
            }
        )
        messages = [message.model_copy(deep=True) for message in candidate.messages]
        messages.append(Message(role="user", content=_PROBE_MESSAGE))
        scope = await build_session_usage_scope(
            self._usage_event_sink,
            self._session_manager,
            key,
            run_kind="prompt_cache_keepalive",
        )
        done_event: Any | None = None
        provider_error = False

        def raw_stream() -> Any:
            return candidate.provider.chat(
                messages,
                tools=list(candidate.tools),
                config=config,
            )

        with bind_usage_accounting_scope(scope):
            if provider_accounts_physical_usage(candidate.provider):
                stream = raw_stream()
            else:
                stream = account_provider_stream(
                    raw_stream,
                    provider=candidate.provider_id,
                    model=candidate.model,
                )
            async for event in stream:
                kind = str(getattr(event, "kind", "") or "")
                if kind == "error":
                    provider_error = True
                elif kind == "done":
                    done_event = event

        current_lease = self._leases.get(key)
        if (
            current_lease is None
            or current_lease.generation != generation
            or not current_lease.enabled
        ):
            return
        current_lease.last_probe_at_ms = int(time.time() * 1000)
        cache_hit_tokens = max(0, int(getattr(done_event, "cached_tokens", 0) or 0))
        current_lease.last_cache_hit_tokens = cache_hit_tokens
        if provider_error or done_event is None:
            self._stop(key, generation, "provider_error")
            return
        if cache_hit_tokens <= 0:
            self._stop(key, generation, "cache_miss_or_usage_unreported")
            return
        current_lease.state = "scheduled"
        current_lease.reason = None
        self._schedule(
            key,
            current_lease,
            delay=current_lease.ttl_seconds * _PROBE_RATIO,
        )

    def _stop(self, key: str, generation: int, reason: str) -> None:
        lease = self._leases.get(key)
        if lease is None or lease.generation != generation:
            return
        lease.enabled = False
        lease.state = "stopped"
        lease.reason = reason
        lease.next_probe_at_ms = None
        lease.timer_task = None
        lease.candidate = None
        lease.idle_deadline_monotonic = None
        lease.idle_expires_at_ms = None

    def _pause_for_idle_timeout(self, key: str, generation: int) -> None:
        lease = self._leases.get(key)
        if lease is None or lease.generation != generation or not lease.enabled:
            return
        lease.state = "paused"
        lease.reason = "idle_timeout"
        lease.next_probe_at_ms = None
        lease.timer_task = None
        lease.candidate = None
        lease.idle_deadline_monotonic = None


__all__ = [
    "DEFAULT_IDLE_TIMEOUT_SECONDS",
    "DEFAULT_TTL_SECONDS",
    "MAX_IDLE_TIMEOUT_SECONDS",
    "MAX_TTL_SECONDS",
    "MIN_IDLE_TIMEOUT_SECONDS",
    "MIN_TTL_SECONDS",
    "PromptCacheKeepaliveService",
]
