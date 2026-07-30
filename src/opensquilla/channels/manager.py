"""ChannelManager — lifecycle management for ManagedChannel adapters."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any

import structlog
from starlette.routing import Route

from opensquilla.channels.registry import build_managed_channel
from opensquilla.channels.types import ChannelHealth, DeliveryTargetResolution, ManagedChannel
from opensquilla.gateway._debounce import _DefaultDebounceCoordinator
from opensquilla.gateway.channel_dispatch import run_channel_dispatch
from opensquilla.session.keys import (
    DmScope,
    build_direct_key,
    build_group_key,
    build_group_sender_key,
    build_thread_key,
)

log = structlog.get_logger(__name__)


def _structured_diagnostic(exc: BaseException) -> dict[str, Any] | None:
    raw = getattr(exc, "diagnostic", None)
    if not isinstance(raw, dict):
        return None
    diagnostic: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, str | int | float | bool) or value is None:
            diagnostic[key] = value
    return diagnostic or None


@dataclass
class ChannelManager:
    """Manages lifecycle of ManagedChannel instances.

    Responsibilities:
    - Build adapters from gateway config entries (from_config)
    - Collect webhook routes for Starlette registration
    - Start/stop/restart individual channels or all at once
    - Run dispatch loops with exponential-backoff retry
    - Build proper session keys via session/keys.py
    """

    _channels: dict[str, ManagedChannel]
    _turn_runner: Any  # TurnRunner (avoid circular import at module level)
    _session_manager: Any  # SessionManager
    _event_bridge: Any = None  # EventBridge | None (injected from gateway boot)
    _config: Any = None
    _task_runtime: Any = None
    _rpc_dispatcher: Any = None
    _channel_rpc_context_factory: Callable[[Any], Any] | None = None
    _delivery_store: Any = None
    _lease_owner_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _transport_leases: dict[str, Any] = field(default_factory=dict)
    _lease_tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    _debounce_coordinator: Any = field(default_factory=_DefaultDebounceCoordinator)
    _agent_ids: dict[str, str] = field(default_factory=dict)
    _channel_types: dict[str, str] = field(default_factory=dict)
    _group_session_scopes: dict[str, str] = field(default_factory=dict)
    _busy_input_modes: dict[str, str] = field(default_factory=dict)
    _tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    # Per-channel in-flight reply task sets.
    # Keyed by channel name; populated in _safe_start and consumed in stop_channel.
    _in_flight_sets: dict[str, Any] = field(default_factory=dict)
    # Dispatch state machine — see _dispatch_with_retry for the lifecycle.
    # Values: "running" | "exhausted" | "restarting" | "dead". Unset entries
    # are treated as "unknown" by health() so a channel that never started
    # does not look healthy.
    _dispatch_states: dict[str, str] = field(default_factory=dict)
    _restart_counts: dict[str, int] = field(default_factory=dict)
    _start_errors: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Stable per-entry config fingerprint, written at install time; reconcile
    # compares it against the live config to decide rebuild vs unchanged.
    _entry_fingerprints: dict[str, str] = field(default_factory=dict)
    # Wall-clock epoch (ms) at which the dispatch loop last entered "running".
    # Surfaced as ``connected_since`` so operators see uptime, cleared on stop.
    _running_since: dict[str, int] = field(default_factory=dict)
    # Leading-edge throttle for channel.status pushes: reconnect thrash can
    # flip dispatch states back-to-back, so coalesce per channel.
    _last_status_emit: dict[str, float] = field(default_factory=dict)
    _status_emit_min_interval: float = 1.0
    # Inner-loop retry policy (overridable for tests).
    _max_retries: int = 5
    _retry_backoff_initial: float = 1.0
    _retry_backoff_max: float = 60.0
    # Outer-loop restart policy. ``dead`` is operator-recoverable via the
    # ``channels.restart`` admin RPC; the cap only bounds *automatic*
    # restart attempts.
    _restart_delay_s: float = 30.0
    _max_restart_cycles: int = 3
    # Serializes every path that mutates runtime channel state (reconcile,
    # restart, stop). Without it, two concurrent CRUD RPCs interleave
    # stop/start on the same name and orphan dispatch/lease tasks.
    _mutate_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ── Factory ──────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        entries: list,
        *,
        turn_runner: Any,
        session_manager: Any,
        event_bridge: Any = None,
        config: Any = None,
        task_runtime: Any = None,
        rpc_dispatcher: Any = None,
        channel_rpc_context_factory: Callable[[Any], Any] | None = None,
    ) -> ChannelManager:
        """Build adapter instances from gateway config entries.

        Each entry's ``type`` field selects the adapter class.
        Disabled entries are skipped.
        """
        from opensquilla.channels.delivery_store import delivery_store_for_config

        manager = cls(
            _channels={},
            _turn_runner=turn_runner,
            _session_manager=session_manager,
            _event_bridge=event_bridge,
            _config=config,
            _task_runtime=task_runtime,
            _rpc_dispatcher=rpc_dispatcher,
            _channel_rpc_context_factory=channel_rpc_context_factory,
            _delivery_store=delivery_store_for_config(config),
            _agent_ids={},
            _channel_types={},
            _group_session_scopes={},
            _busy_input_modes={},
        )
        for entry in entries:
            if not entry.enabled:
                log.info("channel.skipped_disabled", name=entry.name)
                continue
            adapter = build_managed_channel(entry)
            if adapter is None:
                log.warning("channel.unknown_type", type=entry.type, name=entry.name)
                continue
            manager._install_adapter(entry, adapter)
        return manager

    def _install_adapter(self, entry: Any, adapter: ManagedChannel) -> None:
        """Wire a built adapter into the manager — the ONE place this happens.

        Everything an adapter needs beyond its constructor lives here: access
        policy, delivery-store attributes, outbox wrapping, tool registration,
        the per-name side maps, and the entry fingerprint reconcile uses to
        detect config drift.
        """
        from opensquilla.channels._util import ChannelAccessPolicy, ChannelDmAccess
        from opensquilla.channels.delivery_store import install_outbox

        name = entry.name
        self._channels[name] = adapter
        declared_policy = getattr(adapter, "policy", None)
        if not isinstance(declared_policy, ChannelAccessPolicy):
            declared_policy = ChannelAccessPolicy()
        setattr(
            adapter,
            "policy",
            replace(
                declared_policy,
                dm_access=ChannelDmAccess(str(getattr(entry, "dm_access", "pairing"))),
                allowlist=frozenset(getattr(entry, "allowed_senders", ())),
            ),
        )
        setattr(adapter, "_delivery_store", self._delivery_store)
        setattr(adapter, "_delivery_channel_name", name)
        install_outbox(adapter)
        self._register_tool_channel(name, adapter)
        self._agent_ids[name] = getattr(entry, "agent_id", "main")
        self._channel_types[name] = entry.type
        self._group_session_scopes[name] = getattr(entry, "group_session_scope", "per_sender")
        self._busy_input_modes[name] = getattr(entry, "busy_input_mode", "followup")
        setattr(adapter, "debounce_window_s", getattr(entry, "debounce_window_s", 0.0))
        self._entry_fingerprints[name] = self._entry_fingerprint(entry)
        log.info("channel.adapter_created", name=name, type=entry.type)

    @staticmethod
    def _register_tool_channel(name: str, adapter: ManagedChannel) -> None:
        # Channels expose only the vendor-neutral messaging tool. Vendor API
        # surfaces (docs/drive/calendar/...) are the platform vendors' own
        # MCP servers and CLIs, mounted through the MCP client — never
        # bundled per channel.
        try:
            from opensquilla.tools.builtin.messaging import register_channel

            register_channel(name, adapter)
        except Exception as exc:
            log.debug("channel.tool_register_failed", name=name, tool="message", error=str(exc))

    @staticmethod
    def _unregister_tool_channel(name: str, adapter: ManagedChannel | None) -> None:
        try:
            from opensquilla.tools.builtin.messaging import unregister_channel

            unregister_channel(name)
        except Exception as exc:
            log.debug("channel.tool_unregister_failed", name=name, tool="message", error=str(exc))

    # ── Webhook routes ───────────────────────────────────────

    def collect_webhook_routes(self) -> list[Route]:
        """Extract Starlette Routes from adapters that support webhooks.

        Slack and Feishu adapters expose ``create_webhook_route()``;
        Discord uses a persistent WebSocket and has no webhook.
        """
        routes: list[Route] = []
        for name, adapter in self._channels.items():
            if getattr(adapter, "transport_name", "webhook") != "webhook":
                continue
            if hasattr(adapter, "create_webhook_route"):
                route = adapter.create_webhook_route()
                routes.append(route)
                log.info("channel.webhook_route_collected", channel=name, path=route.path)
        return routes

    # ── Lifecycle ────────────────────────────────────────────

    async def start_all(self) -> dict[str, bool]:
        """Start all channels concurrently.

        Returns ``{name: success}`` map.  Partial failures do NOT
        prevent other channels from starting.
        """
        async with self._mutate_lock:
            results = await asyncio.gather(
                *[self._safe_start(name) for name in self._channels],
                return_exceptions=True,
            )
        statuses: dict[str, bool] = {}
        for name, result in zip(self._channels, results):
            if isinstance(result, BaseException):
                details: dict[str, Any] = {
                    "error_type": type(result).__name__,
                    "error": str(result),
                    "exception": repr(result),
                }
                diagnostic = _structured_diagnostic(result)
                if diagnostic:
                    details["diagnostic"] = diagnostic
                self._start_errors[name] = details
                statuses[name] = False
            else:
                self._start_errors.pop(name, None)
                statuses[name] = True
        return statuses

    def start_errors(self) -> dict[str, dict[str, Any]]:
        """Return sanitized per-channel startup errors for operator diagnostics."""
        return {name: dict(details) for name, details in self._start_errors.items()}

    async def _safe_start(self, name: str) -> None:
        """Start a single channel with 30 s timeout, then launch dispatch loop."""
        from opensquilla.gateway.channel_dispatch import _ChannelInFlightSet, _compute_channel_cap

        adapter = self._channels[name]
        lease = None
        if self._delivery_store is not None:
            account_id = self._transport_account_id(name, adapter)
            lease = self._delivery_store.acquire_transport_lease(
                self._channel_types.get(name, name),
                account_id,
                self._lease_owner_id,
            )
            if lease is None:
                raise RuntimeError(f"channel transport lease is already held for {name}")
            self._transport_leases[name] = lease
            setattr(adapter, "_transport_fencing_token", lease.fencing_token)
        startup_timeout = float(getattr(adapter, "startup_timeout_s", 30.0))
        try:
            if lease is not None and self._delivery_store is not None:
                enqueue = getattr(adapter, "enqueue", None)
                if callable(enqueue):
                    for recovered in self._delivery_store.recover_inbound(name):
                        enqueue(recovered)
            self._unregister_tool_channel(name, adapter)
            await asyncio.wait_for(adapter.start(), timeout=startup_timeout)
            self._register_tool_channel(name, adapter)
        except Exception:
            stop = getattr(adapter, "stop", None)
            if callable(stop):
                with contextlib.suppress(Exception):
                    await stop()
            self._unregister_tool_channel(name, adapter)
            if lease is not None and self._delivery_store is not None:
                self._delivery_store.release_transport_lease(lease)
                self._transport_leases.pop(name, None)
            raise
        if lease is not None:
            self._lease_tasks[name] = asyncio.create_task(
                self._renew_transport_lease(name),
                name=f"channel-lease:{name}",
            )
        entry_agent_id = self._agent_ids.get(name, "main")
        key_builder = partial(
            self._build_session_key,
            name,
            agent_id=entry_agent_id,
            group_session_scope=self._group_session_scopes.get(name, "per_sender"),
        )
        cap = _compute_channel_cap(self._config)
        in_flight = _ChannelInFlightSet(cap)
        self._in_flight_sets[name] = in_flight
        self._tasks[name] = asyncio.create_task(
            self._dispatch_with_retry(name, key_builder, in_flight=in_flight),
            name=f"channel:{name}",
        )

    def _transport_account_id(self, name: str, adapter: Any) -> str:
        config = getattr(adapter, "config", None)
        candidates: list[str] = []
        for source in (config, adapter):
            if source is None:
                continue
            for field_name in (
                "app_id",
                "bot_id",
                "corp_id",
                "client_id",
                "user_id",
                "homeserver_url",
                "token",
                "app_token",
            ):
                value = str(getattr(source, field_name, "") or "").strip()
                if value:
                    candidates.append(f"{field_name}={value}")
        if not candidates:
            candidates.append(f"name={name}")
        return hashlib.sha256("\0".join(candidates).encode()).hexdigest()[:24]

    async def _renew_transport_lease(self, name: str) -> None:
        while True:
            await asyncio.sleep(30.0)
            lease = self._transport_leases.get(name)
            if lease is None or self._delivery_store is None:
                return
            renewed = self._delivery_store.renew_transport_lease(lease)
            if renewed is not None:
                self._transport_leases[name] = renewed
                continue
            log.error("channel.transport_lease_lost", channel=name)
            adapter = self._channels.get(name)
            if adapter is not None:
                setattr(adapter, "_connected", False)
                with contextlib.suppress(Exception):
                    await adapter.stop()
            return

    async def _run_one_dispatch_cycle(
        self,
        name: str,
        key_builder: Callable[[Any], str],
        in_flight: Any = None,
    ) -> None:
        """Inner retry loop. Returns once retries are exhausted.

        CRITICAL: ``CancelledError`` always propagates — it signals
        intentional shutdown via ``stop_channel``.  Only ``Exception``
        subclasses trigger retry.
        """
        backoff = self._retry_backoff_initial
        max_backoff = self._retry_backoff_max

        for attempt in range(self._max_retries + 1):
            try:
                await run_channel_dispatch(
                    channel=self._channels[name],
                    turn_runner=self._turn_runner,
                    session_manager=self._session_manager,
                    session_key_builder=key_builder,
                    session_prefix=name,
                    event_bridge=self._event_bridge,
                    config=self._config,
                    task_runtime=self._task_runtime,
                    rpc_dispatcher=self._rpc_dispatcher,
                    channel_rpc_context_factory=self._channel_rpc_context_factory,
                    debounce_coordinator=self._debounce_coordinator,
                    debounce_window_s=getattr(self._channels[name], "debounce_window_s", 0.0),
                    busy_input_mode=self._busy_input_modes.get(name, "followup"),
                    _in_flight=in_flight,
                )
            except asyncio.CancelledError:
                raise  # intentional shutdown — never retry
            except Exception as exc:
                log.error(
                    "channel.dispatch_error",
                    channel=name,
                    attempt=attempt,
                    max_retries=self._max_retries,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)
                else:
                    log.error("channel.dispatch_exhausted", channel=name)

    def _set_dispatch_state(self, name: str, state: str) -> None:
        """Update the dispatch state and push a ``channel.status`` event on change.

        Every configured runtime surface (both channel UIs) subscribes to
        ``channel.status``; emitting on real transitions turns the 30s poll
        into a live view. Emission is best-effort and never blocks dispatch.
        """
        prev = self._dispatch_states.get(name)
        self._dispatch_states[name] = state
        if state == "running":
            # First time running (or resumed after a restart): stamp uptime.
            if prev != "running":
                self._running_since[name] = int(time.time() * 1000)
        elif state in {"dead", "exhausted"}:
            self._running_since.pop(name, None)
        if prev != state:
            self._emit_status_event(name, state)

    def _emit_status_event(self, name: str, state: str) -> None:
        bridge = self._event_bridge
        if bridge is None:
            return
        now = time.monotonic()
        # Terminal states always emit; churny intermediates coalesce.
        if state not in {"dead", "running"}:
            if now - self._last_status_emit.get(name, 0.0) < self._status_emit_min_interval:
                return
        self._last_status_emit[name] = now
        coro = bridge.broadcast_scoped(
            "channel.status",
            {"name": name, "status": state},
            required_scope="operator.read",
        )
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()

    async def _dispatch_with_retry(
        self,
        name: str,
        key_builder: Callable[[Any], str],
        in_flight: Any = None,
    ) -> None:
        """Outer cycle loop wrapping the inner retry budget.

        Each iteration runs one dispatch cycle. After the inner retry budget
        is exhausted the channel transitions through
        ``running → exhausted → restarting → running`` until the configured
        restart cap is hit, at which point it transitions to ``dead`` and
        the loop exits. ``dead`` is operator-recoverable through
        ``restart_channel``.
        """
        self._set_dispatch_state(name, "running")
        self._restart_counts.setdefault(name, 0)
        while True:
            await self._run_one_dispatch_cycle(name, key_builder, in_flight=in_flight)

            self._set_dispatch_state(name, "exhausted")
            log.warning(
                "dispatch.running_to_exhausted",
                channel=name,
                restart_count=self._restart_counts[name],
            )

            if self._restart_counts[name] >= self._max_restart_cycles:
                self._set_dispatch_state(name, "dead")
                log.error(
                    "dispatch.restarting_to_dead",
                    channel=name,
                    restart_count=self._restart_counts[name],
                )
                return

            self._restart_counts[name] += 1
            self._set_dispatch_state(name, "restarting")
            log.warning(
                "dispatch.exhausted_to_restarting",
                channel=name,
                restart_count=self._restart_counts[name],
                max_cycles=self._max_restart_cycles,
            )
            await asyncio.sleep(self._restart_delay_s)
            self._set_dispatch_state(name, "running")

    async def stop_all(self) -> None:
        """Stop every managed channel (dispatch task + adapter)."""
        async with self._mutate_lock:
            for name in list(self._channels):
                await self._stop_channel_locked(name)
        await self._debounce_coordinator.cancel_all()
        if self._delivery_store is not None:
            self._delivery_store.close()
            self._delivery_store = None

    async def stop_channel(self, name: str) -> None:
        """Cancel dispatch task, cancel all in-flight reply tasks, then stop adapter.

        MUST use this instead of calling ``adapter.stop()`` directly,
        otherwise the dispatch task becomes orphaned.

        In-flight reply tasks are cancelled and awaited
        before the adapter is stopped so no dangling coroutines remain after
        shutdown.
        """
        async with self._mutate_lock:
            await self._stop_channel_locked(name)

    async def _stop_channel_locked(self, name: str) -> None:
        task = self._tasks.pop(name, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Cancel and await all in-flight reply tasks for this channel.
        in_flight = self._in_flight_sets.pop(name, None)
        if in_flight is not None:
            await in_flight.cancel_all()
        adapter = self._channels.get(name)
        try:
            if adapter:
                await adapter.stop()
        finally:
            lease_task = self._lease_tasks.pop(name, None)
            if lease_task is not None and not lease_task.done():
                lease_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await lease_task
            lease = self._transport_leases.pop(name, None)
            if lease is not None and self._delivery_store is not None:
                self._delivery_store.release_transport_lease(lease)
            self._unregister_tool_channel(name, adapter)
            self._running_since.pop(name, None)

    async def restart_channel(self, name: str) -> None:
        """Stop then re-start a single channel.

        On a ``dead`` channel this is the operator-recoverable path: the
        restart counter is cleared and a single ``dispatch.dead_to_running``
        decision-log entry is emitted before the new dispatch loop spins up.
        """
        async with self._mutate_lock:
            prev_state = self._dispatch_states.get(name)
            await self._stop_channel_locked(name)
            self._restart_counts[name] = 0
            if prev_state == "dead":
                log.info("dispatch.dead_to_running", channel=name)
            await self._safe_start(name)

    # ── Live reconcile ───────────────────────────────────────

    @staticmethod
    def _entry_fingerprint(entry: Any) -> str:
        """Stable hash of an entry's full configuration."""
        dump = getattr(entry, "model_dump", None)
        payload = dump(mode="json") if callable(dump) else dict(vars(entry))
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()

    @staticmethod
    def _is_webhook_adapter(adapter: Any) -> bool:
        """Webhook-mode adapters register HTTP routes at boot; their routes
        cannot be added or re-pointed on the running app yet, so reconcile
        must leave them to a gateway restart."""
        return (
            getattr(adapter, "transport_name", "webhook") == "webhook"
            and hasattr(adapter, "create_webhook_route")
        )

    def _uninstall_adapter(self, name: str) -> None:
        """Forget a stopped channel's runtime state (inverse of _install_adapter)."""
        self._channels.pop(name, None)
        for side_map in (
            self._agent_ids,
            self._channel_types,
            self._group_session_scopes,
            self._busy_input_modes,
            self._entry_fingerprints,
            self._dispatch_states,
            self._restart_counts,
            self._start_errors,
        ):
            side_map.pop(name, None)

    async def reconcile(self, entries: list) -> dict[str, str]:
        """Make the running adapters match ``entries`` without a process restart.

        Per-name outcomes:
        - ``started``    — new entry built, wired, and started live
        - ``rebuilt``    — config changed; old adapter stopped, new one started
        - ``removed``    — entry gone (or disabled); adapter stopped and forgotten
        - ``unchanged``  — fingerprint identical; adapter untouched
        - ``pending_restart`` — webhook-mode on either side; needs a gateway
          restart (HTTP routes are bound at boot) — nothing was changed live
        - ``failed``     — the new adapter did not start; the entry stays
          installed with its error in ``start_errors`` so ``channels.restart``
          can retry, and the durable ingress journal keeps queued messages

        A start failure never escalates beyond its own channel: the blast
        radius of a bad entry is that entry, surfaced through the existing
        status/doctor surfaces, not a gateway restart.
        """
        desired: dict[str, Any] = {}
        for entry in entries:
            if getattr(entry, "enabled", True):
                desired[entry.name] = entry

        results: dict[str, str] = {}
        async with self._mutate_lock:
            for name in [n for n in list(self._channels) if n not in desired]:
                if self._is_webhook_adapter(self._channels[name]):
                    results[name] = "pending_restart"
                    continue
                await self._stop_channel_locked(name)
                self._uninstall_adapter(name)
                log.info("channel.reconcile_removed", name=name)
                results[name] = "removed"

            for name, entry in desired.items():
                current = self._channels.get(name)
                if current is not None:
                    fingerprint_equal = (
                        self._entry_fingerprints.get(name) == self._entry_fingerprint(entry)
                    )
                    # Fingerprint-equal counts as unchanged only when the
                    # channel actually runs: a previously failed start must be
                    # retried by the operator's identical re-save, not
                    # reported as already applied.
                    if fingerprint_equal and name in self._tasks:
                        results[name] = "unchanged"
                        continue
                    if self._is_webhook_adapter(current):
                        results[name] = "pending_restart"
                        continue

                adapter = build_managed_channel(entry)
                if adapter is None:
                    log.warning("channel.unknown_type", type=entry.type, name=name)
                    results[name] = "failed"
                    continue
                if self._is_webhook_adapter(adapter):
                    results[name] = "pending_restart"
                    continue
                if self._collides_with_pending(name, entry, adapter, results):
                    # Starting this adapter would fence out a still-running
                    # restart-gated adapter that shares its transport account
                    # — the whole migration is restart-gated.
                    results[name] = "pending_restart"
                    continue

                if current is not None:
                    await self._stop_channel_locked(name)
                    self._uninstall_adapter(name)
                self._install_adapter(entry, adapter)
                try:
                    await self._safe_start(name)
                except Exception as exc:  # noqa: BLE001 - per-channel blast radius
                    details: dict[str, Any] = {
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "exception": repr(exc),
                    }
                    diagnostic = _structured_diagnostic(exc)
                    if diagnostic:
                        details["diagnostic"] = diagnostic
                    self._start_errors[name] = details
                    # Drop the fingerprint so an identical re-save retries the
                    # start instead of reading as unchanged.
                    self._entry_fingerprints.pop(name, None)
                    log.warning(
                        "channel.reconcile_start_failed",
                        name=name,
                        error_type=type(exc).__name__,
                    )
                    results[name] = "failed"
                    continue
                self._start_errors.pop(name, None)
                results[name] = "rebuilt" if current is not None else "started"
                log.info("channel.reconcile_applied", name=name, outcome=results[name])

        return results

    def _collides_with_pending(
        self, name: str, entry: Any, adapter: Any, results: dict[str, str]
    ) -> bool:
        """True when starting ``adapter`` would steal the transport lease of a
        restart-gated adapter that keeps running (same type + account)."""
        candidate = (str(entry.type), self._transport_account_id(name, adapter))
        for other, outcome in results.items():
            if outcome != "pending_restart" or other == name:
                continue
            running = self._channels.get(other)
            if running is None:
                continue
            key = (
                self._channel_types.get(other, other),
                self._transport_account_id(other, running),
            )
            if key == candidate:
                return True
        return False

    # ── Health ───────────────────────────────────────────────

    async def health(self) -> dict[str, ChannelHealth]:
        """Return health status for every managed channel.

        Adapter-reported ``ChannelHealth.extra`` is augmented with the
        dispatch-loop state so operators can distinguish "channel dropped a
        message" from "channel is permanently dead pending admin restart".
        """
        out: dict[str, ChannelHealth] = {}
        for name, a in self._channels.items():
            health = await a.health_check()
            health.extra["dispatch_state"] = self._dispatch_states.get(name, "unknown")
            # Mirror runtime telemetry the adapter itself does not report, so
            # the status RPC can surface honest uptime and restart counts.
            health.extra["restart_attempts"] = self._restart_counts.get(name, 0)
            running_since = self._running_since.get(name)
            if running_since is not None:
                health.extra.setdefault("connected_since", running_since)
            lease = self._transport_leases.get(name)
            if lease is not None:
                health.extra["transport_lease"] = {
                    "owner_id": lease.owner_id,
                    "fencing_token": lease.fencing_token,
                    "expires_at": lease.expires_at,
                }
            out[name] = health
        return out

    # ── Accessors ────────────────────────────────────────────

    def items(self):  # noqa: ANN201
        """Iterate ``(name, adapter)`` pairs."""
        return self._channels.items()

    def get(self, name: str) -> ManagedChannel | None:
        """Look up an adapter by name."""
        return self._channels.get(name)

    def resolve_delivery_target(
        self,
        *,
        target: str,
        to: str = "",
        account_id: str = "",
        thread_id: str = "",
    ) -> DeliveryTargetResolution:
        """Resolve delivery fields to a concrete adapter.

        ``target`` may be an OpenSquilla adapter entry name, or a
        channel type such as ``slack`` when the type maps to one adapter.
        ``account_id`` currently selects a concrete entry until opensquilla grows a
        first-class multi-account channel config.
        """

        target_name = target.strip()
        target_type = target_name.lower()
        account = account_id.strip()
        to = to.strip()
        thread = thread_id.strip()

        if not target_name:
            return DeliveryTargetResolution(ok=False, reason="unsupported_target")

        candidates = [
            name
            for name, channel_type in self._channel_types.items()
            if channel_type.lower() == target_type
        ]
        if account:
            if account not in candidates:
                return DeliveryTargetResolution(ok=False, reason="unsupported_account")
            return self._build_delivery_resolution(
                adapter_name=account,
                channel_type=target_type,
                to=to,
                account_id=account,
                thread_id=thread,
            )

        if target_name in self._channels:
            adapter_name = target_name
            channel_type = self._channel_types.get(adapter_name, adapter_name).lower()
            return self._build_delivery_resolution(
                adapter_name=adapter_name,
                channel_type=channel_type,
                to=to,
                account_id=account,
                thread_id=thread,
            )

        if not candidates:
            return DeliveryTargetResolution(ok=False, reason="unsupported_target")
        if len(candidates) > 1:
            return DeliveryTargetResolution(ok=False, reason="ambiguous_account")

        return self._build_delivery_resolution(
            adapter_name=candidates[0],
            channel_type=target_type,
            to=to,
            account_id=account,
            thread_id=thread,
        )

    def _build_delivery_resolution(
        self,
        *,
        adapter_name: str,
        channel_type: str,
        to: str,
        account_id: str,
        thread_id: str,
    ) -> DeliveryTargetResolution:
        if thread_id and channel_type not in {"slack"}:
            return DeliveryTargetResolution(ok=False, reason="unsupported_thread")
        return DeliveryTargetResolution(
            ok=True,
            adapter=self._channels.get(adapter_name),
            adapter_name=adapter_name,
            channel_type=channel_type,
            to=to,
            account_id=account_id,
            thread_id=thread_id,
        )

    # ── Session key builder ──────────────────────────────────

    @staticmethod
    def _build_session_key(
        channel_name: str,
        msg: Any,
        agent_id: str = "main",
        group_session_scope: str = "per_sender",
    ) -> str:
        """Build a proper session key using ``session/keys.py`` builders.

        Detects group vs DM from message metadata:
        - Feishu: ``metadata.chat_type == "group"``
        - Discord: ``metadata.guild_id is not None``
        - Slack: ``metadata.channel_type in ("channel", "group")``

        Group keys use ``msg.channel_id`` as peer_id and, by default,
        ``msg.sender_id`` to isolate participants in the same room.
        ``group_session_scope='shared_room'`` preserves the explicit
        compatibility mode where every participant shares one transcript.
        DM keys use ``msg.sender_id`` as peer_id (per-user session).
        """
        meta = getattr(msg, "metadata", {}) or {}
        flag = meta.get("is_group")
        if flag is not None:
            is_group = bool(flag)
        else:
            # Adapter metadata fallback for events that do not yet carry the
            # ``metadata['is_group']`` contract documented in ``channels/types.py``.
            is_group = (
                meta.get("chat_type") == "group"  # Feishu
                or meta.get("guild_id") is not None  # Discord
                or meta.get("channel_type") in ("channel", "group")  # Slack
            )

        if is_group:
            if group_session_scope == "shared_room":
                base_key = build_group_key(
                    agent_id=agent_id,
                    channel=channel_name,
                    peer_id=msg.channel_id,
                )
            else:
                base_key = build_group_sender_key(
                    agent_id=agent_id,
                    channel=channel_name,
                    peer_id=msg.channel_id,
                    sender_id=msg.sender_id,
                )
            thread_id = (
                meta.get("native_thread_id") or meta.get("thread_ts") or meta.get("thread_id")
            )
            if isinstance(thread_id, str) and thread_id:
                return build_thread_key(base_key, thread_id, channel_hint=channel_name)
            return base_key

        return build_direct_key(
            agent_id=agent_id,
            channel=channel_name,
            peer_id=msg.sender_id,
            dm_scope=DmScope.PER_CHANNEL_PEER,
        )
