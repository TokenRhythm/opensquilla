"""Channel-to-agent bridge: receive-dispatch-respond loop with helpers.

The main ``run_channel_dispatch`` function is a thin orchestrator (~25 lines)
that delegates to private helpers for each concern:

- ``_record_delivery_context`` — persist routing fields on session (Gap 1)
- ``_should_skip_unmentioned`` — mention gating for groups (Gap 2)
- ``_start_typing_keepalive`` — background typing indicator (Gap 3)
- ``_run_turn_with_streaming`` — streaming or batch reply (Gap 4)
- ``_emit_events`` — broadcast session events to WS subscribers (Gap 5)
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog

from opensquilla.agents.scope import resolve_agent_model
from opensquilla.artifacts import artifact_payload
from opensquilla.channels._util import (
    measured_len,
    sender_is_channel_admin,
    split_text_for_channel,
    truncate_to_limit,
)
from opensquilla.channels.admission import (
    CHANNEL_ADMIN_VERIFIED_METADATA_KEY,
    ChannelAdmissionDecision,
    decide_channel_admission,
    has_verified_channel_admin_stamp,
    is_authenticated_channel_admin,
)
from opensquilla.channels.artifact_delivery import (
    artifact_delivery_key as _artifact_delivery_key,
)
from opensquilla.channels.artifact_delivery import (
    artifact_fallback_lines as _artifact_fallback_lines,
)
from opensquilla.channels.artifact_delivery import (
    can_deliver_channel_files as _can_deliver_channel_files,
)
from opensquilla.channels.artifact_delivery import (
    deliver_artifacts_as_channel_files as _deliver_artifacts_as_channel_files,
)
from opensquilla.channels.artifact_delivery import (
    strip_artifact_markers_from_channel_text as _strip_artifact_markers_from_channel_text,
)
from opensquilla.channels.artifact_delivery import (
    strip_delivered_artifact_image_references as _strip_delivered_artifact_image_references,
)
from opensquilla.channels.contract import (
    REQUIRED_RETRYABLE_ERROR_CLASSES,
    UNCLASSIFIED_ERROR_CLASS,
    channel_capability_profile,
    classify_channel_send_error,
)
from opensquilla.channels.stream_policy import resolve_channel_stream_policy
from opensquilla.channels.system_messages import render_channel_message
from opensquilla.channels.types import IncomingMessage, OutgoingMessage
from opensquilla.engine.start_turn import reserve_turn_via_runtime, start_turn_via_runtime
from opensquilla.engine.types import (
    AnswerGenerationResetEvent,
    ArtifactEvent,
    DoneEvent,
    EnsembleProgressEvent,
    ErrorEvent,
    RouterDecisionEvent,
    RunHeartbeatEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    done_text_snapshot,
)
from opensquilla.execution_status import normalize_execution_status
from opensquilla.gateway.attachment_ingest import AttachmentIngestResult, ingest_attachments
from opensquilla.gateway.config import effective_agent_stream_idle_timeout_seconds
from opensquilla.gateway.session_events import build_sessions_changed_payload
from opensquilla.gateway.turn_ingress import complete_durable_ingress
from opensquilla.paths import media_root_from_config
from opensquilla.permissions import configured_default_elevated
from opensquilla.session.terminal_reply import append_error_ref, build_terminal_reply

if TYPE_CHECKING:
    from opensquilla.gateway.event_bridge import EventBridge

log = structlog.get_logger(__name__)

_CHANNEL_BUSY_INPUT_MODES = frozenset({"followup", "queue", "steer", "interrupt"})


@dataclass(frozen=True)
class _StreamedMessageHandle:
    """A platform message id pinned to the route that created it.

    Built-in adapters intentionally keep returning their historical string
    ids.  Dispatch wraps that result with the exact ``send_streaming`` route
    kwargs so a later terminal edit/delete cannot fall back to mutable
    adapter-global state or a statically configured default conversation.
    """

    message_id: str
    route_kwargs: dict[str, Any]


def _streamed_message_handle(
    result: Any,
    route_kwargs: dict[str, Any],
) -> _StreamedMessageHandle | None:
    if isinstance(result, _StreamedMessageHandle):
        return result
    if not isinstance(result, str) or not result:
        return None
    return _StreamedMessageHandle(
        message_id=result,
        route_kwargs=dict(route_kwargs),
    )


def _channel_can_replace_streamed_text(channel: Any) -> bool:
    """Whether a live preview can be replaced by its terminal snapshot.

    Generic edit support is insufficient: ``send_streaming`` must also promise
    a stable id for the message it created. Unknown/custom adapters and typed
    adapters without that stronger declaration buffer deltas until Done, so a
    conflicting or explicitly empty snapshot cannot leave stale output behind.
    """

    if resolve_channel_stream_policy(channel).mode == "final_only":
        return False
    has_edit = callable(getattr(channel, "edit", None))
    profile = channel_capability_profile(channel)
    return bool(
        profile is not None and profile.edit and profile.streamed_message_replacement and has_edit
    )


def _sanitize_streamed_channel_text(text: str) -> str:
    sanitizer = _DirectiveTagStreamSanitizer()
    cleaned = sanitizer.clean(_strip_artifact_markers_from_channel_text(text))
    return cleaned + sanitizer.flush()


async def _replace_streamed_channel_text(
    channel: Any,
    raw_handle: Any,
    text: str,
) -> bool:
    """Best-effort route-pinned replacement of an already delivered preview."""

    handle = (
        raw_handle
        if isinstance(raw_handle, _StreamedMessageHandle)
        else _streamed_message_handle(raw_handle, {})
    )
    if handle is None:
        return False

    def _route_kwargs(operation: Any) -> dict[str, Any]:
        return {
            key: value
            for key, value in handle.route_kwargs.items()
            if _accepts_keyword_arg(operation, key)
        }

    try:
        if not text:
            delete = getattr(channel, "delete", None)
            if callable(delete):
                await delete(handle.message_id, **_route_kwargs(delete))
                return True
        edit = getattr(channel, "edit", None)
        if not callable(edit):
            return False
        await edit(handle.message_id, text, **_route_kwargs(edit))
        return True
    except Exception as exc:  # noqa: BLE001 - caller has a canonical batch fallback.
        log.warning(
            "channel_dispatch.stream_terminal_reconcile_failed",
            channel_type=type(channel).__name__,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return False


def _terminal_payload_from_exception(exc: BaseException) -> dict[str, str]:
    is_timeout = isinstance(exc, TimeoutError)
    return {
        "status": "timeout" if is_timeout else "failed",
        "terminal_reason": "timeout" if is_timeout else "error",
        "error_class": exc.__class__.__name__,
        "error_message": str(exc),
    }


def _terminal_payload_from_error_event(event: ErrorEvent) -> dict[str, str | None]:
    code = (event.code or "").lower()
    is_timeout = "timeout" in code or "stream_idle" in code
    return {
        "status": "timeout" if is_timeout else "failed",
        "terminal_reason": "timeout" if is_timeout else "error",
        "error_class": event.code,
        "error_message": event.message,
    }


def _terminal_reply_suffix(message: str) -> str:
    return f"\n\n({message})"


def _emit_metric(name: str, value: int = 1, **labels: Any) -> None:
    """Emit a structured log line for a core metric (mirrors task_runtime._emit_metric).

    Format: event=<name> metric=<name> value=<int> [labels...]
    Used here for channel-adapter-level counters (queue_full_errors_total,
    turn_cancellations_total) that originate outside task_runtime.  Kept as a
    local copy to avoid a routing→task_runtime→channel_dispatch import cycle.
    """
    log.info(name, metric=name, value=value, **labels)


def _resolve_channel_overflow_policy(channel: Any, config: Any) -> str | None:
    """Resolve the per-channel overflow policy override (if any).

    Reads ``config.task_runtime.pending_overflow_policy_per_channel`` keyed
    by ``channel.channel_id``. Returns ``None`` when the channel has no
    explicit override so ``runtime.enqueue`` falls back to its constructor
    default (typically the global ``pending_overflow_policy``).
    """
    if config is None:
        return None
    runtime_cfg = getattr(config, "task_runtime", None)
    overrides = getattr(runtime_cfg, "pending_overflow_policy_per_channel", None)
    if not overrides:
        return None
    channel_id = getattr(channel, "channel_id", None)
    if not isinstance(channel_id, str) or not channel_id:
        return None
    value = overrides.get(channel_id)
    if not isinstance(value, str) or not value:
        return None
    return value


def _resolve_channel_busy_input_mode(task_runtime: Any, configured_mode: str) -> str:
    """Resolve a channel busy-input policy against runtime capabilities.

    Invalid adapter state and runtimes that do not advertise exact support
    fail closed to the historical ``followup`` behavior.
    """
    mode = str(configured_mode or "followup").strip().lower()
    if mode not in _CHANNEL_BUSY_INPUT_MODES:
        log.warning(
            "channel_dispatch.invalid_busy_input_mode",
            configured_mode=mode,
            fallback="followup",
        )
        return "followup"
    supports_mode = getattr(task_runtime, "supports_queue_mode", None)
    if callable(supports_mode):
        try:
            if supports_mode(mode):
                return mode
        except Exception:
            pass
    elif mode == "followup":
        return mode
    log.warning(
        "channel_dispatch.unsupported_busy_input_mode",
        configured_mode=mode,
        fallback="followup",
    )
    return "followup"


class _ChannelInFlightSet:
    """Per-channel in-flight reply task tracker with a configurable cap.

    This is a SEPARATE second-layer semaphore from ``task_runtime._global_sem``.
    ``task_runtime._global_sem`` gates how many turns run concurrently across
    all sessions; this cap gates how many *channel reply deliveries* are
    outstanding on a single channel adapter concurrently.  The two semaphores
    are independent: a turn can be enqueued in task_runtime but its reply
    delivery may still be queued here waiting for an in-flight slot.

    Cap formula: ``min(channel_inflight_cap, max(2 × max_concurrency, 1))``
    This prevents the channel adapter layer from exhausting the global semaphore
    by ensuring the channel cap never exceeds twice the global concurrency budget.

    Env variable: ``OPENSQUILLA_CHANNEL_INFLIGHT_CAP`` (default 8) is
    surfaced through ``config.task_runtime.channel_inflight_cap``.
    """

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def cap(self) -> int:
        return self._cap

    def full(self) -> bool:
        return len(self._tasks) >= self._cap

    def add(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)

    def discard(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)

    def try_acquire(self, token: object) -> bool:
        """Atomically check cap and reserve a slot using *token* as the key.

        Returns True and adds *token* to the set if the cap is not yet reached;
        returns False (no mutation) if the set is already full.  Because asyncio
        runs on a single thread, this check-then-add pair is atomic — no await
        occurs between the guard and the mutation.
        """
        if len(self._tasks) >= self._cap:  # type: ignore[arg-type]
            return False
        self._tasks.add(token)  # type: ignore[arg-type]
        return True

    def release(self, token: object) -> None:
        """Release a reservation previously acquired via try_acquire."""
        self._tasks.discard(token)  # type: ignore[arg-type]

    async def cancel_all(self) -> None:
        """Cancel every in-flight task and await completion (for shutdown)."""
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


def _compute_channel_cap(config: Any) -> int:
    """Compute the effective per-channel in-flight cap.

    Formula: ``min(channel_inflight_cap, max(2 × max_concurrency, 1))``

    This avoids the channel adapter layer monopolising the global semaphore
    (``task_runtime._global_sem``) whose size equals ``max_concurrency``.
    """
    task_runtime_cfg = getattr(config, "task_runtime", None) if config is not None else None
    raw_cap: int = getattr(task_runtime_cfg, "channel_inflight_cap", 8)
    max_concurrency: int = getattr(task_runtime_cfg, "max_concurrency", 8)
    formula_cap = max(2 * max_concurrency, 1)
    return min(raw_cap, formula_cap)


_DIRECTIVE_TAG_RE = re.compile(r"\[\[\s*(?:reply_to_current|reply_to\s*:\s*[^\]\n]+)\s*\]\]\s*")
_INTERNAL_COMPACTION_MARKER_RE = re.compile(
    r"(?m)^[ \t]*\["
    r"(?:opensquilla_compacted:[^\]\r\n]*|"
    r"provider_request_[^\]\r\n]*compacted:[^\]\r\n]*)"
    r"\][ \t]*(?:\r?\n)?"
    r"|\[(?:opensquilla_compacted:[^\]\r\n]*|"
    r"provider_request_[^\]\r\n]*compacted:[^\]\r\n]*)\]"
)
_INTERNAL_COMPACTION_MARKER_PREFIXES = (
    "[opensquilla_compacted:",
    "[provider_request_",
)
_DIRECTIVE_TAG_BUFFER_LIMIT = 256
_DEFAULT_STREAM_HEARTBEAT_INTERVAL_SECONDS = 15.0


def _strip_inline_directive_tags(content: str) -> str:
    return _DIRECTIVE_TAG_RE.sub("", content)


def _strip_internal_compaction_markers(content: str) -> str:
    return _INTERNAL_COMPACTION_MARKER_RE.sub("", content)


def _split_pending_internal_compaction_marker(content: str) -> tuple[str, str]:
    start = content.rfind("[")
    if start == -1:
        return content, ""
    suffix = content[start:]
    if "\n" in suffix or "\r" in suffix or "]" in suffix:
        return content, ""
    if len(suffix) > _DIRECTIVE_TAG_BUFFER_LIMIT:
        return content, ""
    if any(
        prefix.startswith(suffix) or suffix.startswith(prefix)
        for prefix in _INTERNAL_COMPACTION_MARKER_PREFIXES
    ):
        return content[:start], suffix
    return content, ""


def _sanitize_outgoing_message(message: OutgoingMessage) -> OutgoingMessage:
    cleaned = _strip_internal_compaction_markers(_strip_inline_directive_tags(message.content))
    if cleaned == message.content:
        return message
    return message.model_copy(update={"content": cleaned})


class _DirectiveTagStreamSanitizer:
    """Strip inline reply directives even when a tag is split across chunks."""

    def __init__(self) -> None:
        self._pending = ""

    def clean(self, chunk: str) -> str:
        text = self._pending + chunk
        self._pending = ""
        cleaned = _strip_internal_compaction_markers(_strip_inline_directive_tags(text))
        start = cleaned.rfind("[[")
        if start == -1:
            cleaned, pending_marker = _split_pending_internal_compaction_marker(cleaned)
            if pending_marker:
                self._pending = pending_marker
            return cleaned
        suffix = cleaned[start:]
        if "]]" not in suffix and "\n" not in suffix and len(suffix) <= _DIRECTIVE_TAG_BUFFER_LIMIT:
            self._pending = suffix
            return cleaned[:start]
        cleaned, pending_marker = _split_pending_internal_compaction_marker(cleaned)
        if pending_marker:
            self._pending = pending_marker
            return cleaned
        return cleaned

    def flush(self) -> str:
        pending = self._pending
        self._pending = ""
        return _strip_internal_compaction_markers(_strip_inline_directive_tags(pending))


def _accepts_keyword_arg(callable_obj: Any, name: str) -> bool:
    try:
        params = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


@contextlib.asynccontextmanager
async def _maybe_lock(lock: asyncio.Lock | None) -> AsyncIterator[None]:
    """Yield under ``lock`` if provided; otherwise yield unlocked.

    Defensive helper for paths where ``turn_runner`` may be ``None`` (test
    shims). Mirrors the pattern in ``rpc_sessions._handle_sessions_send``.
    """
    if lock is None:
        yield
        return
    async with lock:
        yield


def _channel_acceptance_lock(
    session_lock: asyncio.Lock | None,
    *,
    atomic: bool,
) -> contextlib.AbstractAsyncContextManager[None]:
    """Keep filesystem validation out of the legacy per-session runner lock."""

    return _maybe_lock(None if atomic else session_lock)


# ── Main dispatch loop (thin orchestrator) ───────────────────────────────


async def run_channel_dispatch(
    channel: Any,
    turn_runner: Any,
    session_manager: Any,
    session_key_builder: Callable[[Any], str],
    session_prefix: str,
    event_bridge: EventBridge | None = None,
    config: Any = None,
    task_runtime: Any = None,
    rpc_dispatcher: Any = None,
    channel_rpc_context_factory: Callable[[Any], Any] | None = None,
    debounce_coordinator: Any = None,
    debounce_window_s: float = 0.0,
    busy_input_mode: str = "followup",
    _in_flight: _ChannelInFlightSet | None = None,
) -> None:
    """Receive-dispatch-respond loop for a channel adapter.

    Runs forever, processing one message at a time.  Each concern is
    handled by a private helper to keep this function under ~25 lines.

    Reply delivery is fire-and-forget via ``asyncio.create_task``; the
    per-channel ``_ChannelInFlightSet`` (a SEPARATE second-layer semaphore
    from ``task_runtime._global_sem``) caps concurrent deliveries.
    """
    if _in_flight is None:
        cap = _compute_channel_cap(config)
        _in_flight = _ChannelInFlightSet(cap)
    while True:
        msg = await channel.receive()
        delivery_store = getattr(channel, "_delivery_store", None)
        delivery_channel_name = str(
            getattr(channel, "_delivery_channel_name", session_prefix) or session_prefix
        )
        ingress_claim = None
        if delivery_store is not None:
            ingress_claim = delivery_store.claim_inbound(delivery_channel_name, msg)
            if ingress_claim is None:
                log.info(
                    "channel.ingress_duplicate_skipped",
                    channel=delivery_channel_name,
                )
                continue
        session_key = session_key_builder(msg)
        admission = decide_channel_admission(
            channel,
            msg,
            session_key,
            config=config,
            channel_name=session_prefix,
        )
        if not admission.admit:
            log.info(
                "channel.admission_denied",
                channel=session_prefix,
                reason=admission.reason,
                is_group=admission.is_group,
            )
            if (
                admission.reason == "pairing_required"
                and admission.pairing_notice
                and admission.pairing_id
            ):
                from opensquilla.gateway.routing import build_channel_route_envelope

                route_envelope = build_channel_route_envelope(
                    msg,
                    session_key=session_key,
                    session_prefix=session_prefix,
                )
                pairing_code = admission.pairing_id[:8]

                notice = _route_envelope_reply_message(
                    render_channel_message(
                        "pairing_required",
                        config=config,
                        pairing_code=pairing_code,
                    ),
                    route_envelope,
                    metadata={"pairing_required": True, "pairing_code": pairing_code},
                )
                try:
                    await channel.send(notice)
                except Exception as exc:
                    log.warning(
                        "channel.pairing_notice_failed",
                        channel=session_prefix,
                        error_type=type(exc).__name__,
                    )
            if delivery_store is not None:
                delivery_store.complete_inbound(
                    ingress_claim,
                    "admission_denied",
                    reason=admission.reason,
                    scrub_payload=True,
                )
            continue
        raw_content = msg.content
        from opensquilla.gateway.routing import build_channel_route_envelope

        route_envelope = build_channel_route_envelope(
            msg,
            session_key=session_key,
            session_prefix=session_prefix,
        )
        principal_is_owner = _stamp_channel_admin_principal(config, route_envelope, msg)
        approval_reply = await _maybe_resolve_channel_approval(
            msg=msg,
            session_key=session_key,
            config=config,
            session_manager=session_manager,
            route_envelope=route_envelope,
        )
        if approval_reply is not None:
            try:
                await channel.send(_preserve_route_channel_metadata(approval_reply, route_envelope))
            except Exception as exc:  # noqa: BLE001 - reply delivery is best-effort
                # The approval outcome is already recorded; a failed reply
                # send must not escape the loop and burn the channel's
                # restart budget.
                log.warning(
                    "channel.approval_reply_send_failed",
                    channel=session_prefix,
                    error_type=type(exc).__name__,
                )
            if delivery_store is not None:
                delivery_store.complete_inbound(
                    ingress_claim, "approval_resolved", reason=admission.reason
                )
            continue
        # fmt: off
        if getattr(channel, "supports_slash_commands", False) and rpc_dispatcher is not None and channel_rpc_context_factory is not None:  # noqa: E501
            command_reply = await _dispatch_channel_slash_command(
                route_envelope=route_envelope, msg=msg, session_manager=session_manager, session_key=session_key, session_prefix=session_prefix, rpc_dispatcher=rpc_dispatcher, context_factory=channel_rpc_context_factory, config=config  # noqa: E501
            )
            if command_reply is not None:
                emit = log.warning if command_reply.metadata.get("denied") else log.info
                if command_reply.metadata.get("denied"):
                    event = "channel.command_denied"
                elif command_reply.metadata.get("unsupported"):
                    event = "channel.command_unsupported"
                else:
                    event = "channel.command_intercepted"
                emit(event, command=command_reply.metadata.get("command"), method=command_reply.metadata.get("method"), session_key=session_key)  # noqa: E501
                # Guarded like turn replies: a provider send failure here must
                # not escape the loop and burn the channel's restart budget.
                await _deliver_reply_or_notify(
                    channel,
                    command_reply,
                    route_envelope=route_envelope,
                    session_key=session_key,
                )
                if delivery_store is not None:
                    delivery_store.complete_inbound(
                        ingress_claim, "command_dispatched", reason=admission.reason
                    )
                continue
        # fmt: on

        # fmt: off
        if task_runtime is not None and debounce_window_s > 0.0 and debounce_coordinator is not None and delivery_store is None:  # noqa: E501
            async def _on_debounce_fire(
                combined: Any,
                key: str = session_key,
                _ifl: _ChannelInFlightSet = cast(_ChannelInFlightSet, _in_flight),
                _admission: ChannelAdmissionDecision = admission,
            ) -> None:
                await _dispatch_combined_message_after_debounce(channel, combined, turn_runner, session_manager, key, session_prefix, task_runtime, config, event_bridge, _ifl, channel_rpc_context_factory=channel_rpc_context_factory, admission_decision=_admission, busy_input_mode=busy_input_mode)  # noqa: E501

            acquire_intent = getattr(
                task_runtime,
                "acquire_explicit_ingress_intent",
                None,
            )
            intent_lease = (
                await acquire_intent(session_key) if callable(acquire_intent) else None
            )

            async def _release_debounce_intent(
                _lease: Any = intent_lease,
            ) -> None:
                if _lease is not None:
                    await _lease.release()

            try:
                await debounce_coordinator.schedule(
                    session_key,
                    msg,
                    window_s=debounce_window_s,
                    on_fire=_on_debounce_fire,
                    on_settled=_release_debounce_intent,
                )
            except BaseException:
                await _release_debounce_intent()
                raise
            continue
        # fmt: on

        # Tier 2 (ADR 008): per-session keyed-async-queue. The same
        # ``turn_runner._get_session_lock(key)`` registry used by
        # ``rpc_sessions.{send,reset}`` gates channel delivery context and
        # transcript append. Remote attachment downloads intentionally run
        # outside this lock; adapter resolvers enforce bounded reads before
        # the locked persistence step.
        _get_lock = getattr(turn_runner, "_get_session_lock", None)
        session_lock = _get_lock(session_key) if callable(_get_lock) else None
        if session_lock is not None and session_lock.locked():
            log.info("channel_dispatch.session_lock_wait", session_key=session_key)
        atomic_channel_acceptance = _supports_atomic_channel_acceptance(
            session_manager,
            task_runtime,
        )

        async with _maybe_lock(session_lock):
            # Mention gating already ran as part of decide_channel_admission
            # at the top of the loop; denied messages never reach this point.
            if not atomic_channel_acceptance:
                # Legacy runners need the session before execution. Production
                # TaskRuntime creates it inside the acceptance transaction.
                await _record_delivery_context(
                    session_manager,
                    session_key,
                    msg,
                    session_prefix,
                    route_envelope=route_envelope,
                )

        ingested: AttachmentIngestResult | None = None
        if not atomic_channel_acceptance:
            await _apply_saved_channel_run_context(
                route_envelope,
                session_manager=session_manager,
                config=config,
                workspace_dir=None,
                principal_is_owner=principal_is_owner,
            )

            ingested = await _ingest_channel_message_attachments(
                channel=channel, msg=msg, config=config
            )

        if not atomic_channel_acceptance:
            async with _maybe_lock(session_lock):
                await _record_delivery_context(
                    session_manager,
                    session_key,
                    msg,
                    session_prefix,
                    route_envelope=route_envelope,
                )

        status_reactor = _status_reactor(channel)
        await status_reactor.received(msg)

        if task_runtime is not None:
            from opensquilla.gateway.task_runtime import TaskQueueFullError

            # Cap check BEFORE enqueue/append: reject early so no transcript
            # entry is written and no runtime turn is started when the channel
            # adapter is already at capacity (accept-then-drop fix).
            if _in_flight.full():
                _emit_metric(
                    "queue_full_errors_total",
                    value=1,
                    session_key=session_key,
                )
                log.warning(
                    "channel_dispatch.inflight_cap_reached",
                    session_key=session_key,
                    cap=_in_flight.cap,
                )
                try:
                    await channel.send(
                        _route_envelope_reply_message(
                            "Server busy, please retry",
                            route_envelope,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - notice is best-effort
                    # The rejection is already decided; a failed busy notice
                    # must not escape the loop and burn the channel's restart
                    # budget while the adapter is already saturated.
                    log.warning(
                        "channel_dispatch.busy_notice_send_failed",
                        session_key=session_key,
                        error_type=type(exc).__name__,
                    )
                await status_reactor.completed(msg)
                if delivery_store is not None:
                    delivery_store.complete_inbound(
                        ingress_claim, "capacity_rejected", reason=admission.reason
                    )
                continue

            transcript_watermark = await _transcript_watermark(session_manager, session_key)
            stream_relay = None
            replayed = False
            try:
                async with _channel_acceptance_lock(
                    session_lock,
                    atomic=atomic_channel_acceptance,
                ):
                    if atomic_channel_acceptance:
                        (
                            handle,
                            persisted_content,
                            stream_relay,
                            replayed,
                        ) = await _accept_channel_runtime_turn(
                            channel=channel,
                            msg=msg,
                            session_manager=session_manager,
                            session_key=session_key,
                            route_envelope=route_envelope,
                            task_runtime=task_runtime,
                            ingested=ingested,
                            raw_content=raw_content,
                            config=config,
                            principal_is_owner=principal_is_owner,
                            busy_input_mode=busy_input_mode,
                        )
                    else:
                        assert ingested is not None
                        stream_relay = _RuntimeChannelStreamRelay.maybe_start(
                            channel,
                            msg,
                            task_runtime,
                            config,
                        )
                        channel_overflow_policy = _resolve_channel_overflow_policy(channel, config)
                        if channel_overflow_policy is not None:
                            apply_policy = getattr(task_runtime, "apply_overflow_policy", None)
                            if callable(apply_policy):
                                await apply_policy(session_key, policy=channel_overflow_policy)
                        handle = await start_turn_via_runtime(
                            task_runtime,
                            route_envelope,
                            msg.content,
                            attachments=ingested.attachments,
                            mode=_resolve_channel_busy_input_mode(task_runtime, busy_input_mode),
                            run_kind="channel_turn",
                            semantic_message=raw_content,
                            stream_event_sink=(
                                stream_relay.emit if stream_relay is not None else None
                            ),
                        )
                        _persisted, persisted_content = await _append_channel_user_message(
                            session_manager=session_manager,
                            session_key=session_key,
                            text=ingested.text,
                            attachments=ingested.attachments,
                            config=config,
                        )
                    msg.content = persisted_content
            except Exception as exc:
                if stream_relay is not None:
                    await stream_relay.close()

                from opensquilla.project_workspaces import ProjectWorkspaceStateError
                from opensquilla.session.storage import (
                    StaleEpochError,
                    StorageBusyError,
                    TurnIngressConflictError,
                )

                if isinstance(exc, StorageBusyError):
                    await status_reactor.failed(msg)
                    await channel.send(
                        _route_envelope_reply_message(
                            "Session storage is busy. Please retry this message.",
                            route_envelope,
                        )
                    )
                    if delivery_store is not None:
                        delivery_store.fail_inbound(ingress_claim, exc)
                    continue
                if isinstance(exc, StaleEpochError):
                    await status_reactor.failed(msg)
                    await channel.send(
                        _route_envelope_reply_message(
                            "The session changed while accepting this message. Please retry.",
                            route_envelope,
                        )
                    )
                    if delivery_store is not None:
                        delivery_store.fail_inbound(ingress_claim, exc)
                    continue
                if isinstance(exc, TurnIngressConflictError):
                    await status_reactor.failed(msg)
                    log.warning(
                        "channel.ingress_idempotency_conflict",
                        session_key=session_key,
                    )
                    await channel.send(
                        _route_envelope_reply_message(
                            "This channel message id was already used; the duplicate was ignored.",
                            route_envelope,
                        )
                    )
                    if delivery_store is not None:
                        delivery_store.complete_inbound(
                            ingress_claim, "ingress_conflict", reason=admission.reason
                        )
                    continue
                if isinstance(exc, ProjectWorkspaceStateError):
                    await status_reactor.failed(msg)
                    workspace_message = (
                        "Project workspace not found. Restore it and retry."
                        if exc.reason in {"not_found", "removed"}
                        else "The project workspace is unavailable. Restore access and retry."
                    )
                    await channel.send(
                        _route_envelope_reply_message(
                            workspace_message,
                            route_envelope,
                        )
                    )
                    if delivery_store is not None:
                        delivery_store.fail_inbound(ingress_claim, exc)
                    continue
                if not isinstance(exc, TaskQueueFullError):
                    if delivery_store is not None:
                        delivery_store.fail_inbound(ingress_claim, exc)
                    raise
                await status_reactor.failed(msg)
                await channel.send(
                    _route_envelope_reply_message(
                        (
                            "The session task queue is full. "
                            f"Try again after queued work completes. ({exc})"
                        ),
                        route_envelope,
                    )
                )
                if delivery_store is not None:
                    delivery_store.complete_inbound(
                        ingress_claim, "queue_rejected", reason=admission.reason
                    )
            else:
                if replayed and handle is None:
                    await status_reactor.completed(msg)
                    if delivery_store is not None:
                        delivery_store.complete_inbound(
                            ingress_claim, "turn_replayed", reason=admission.reason
                        )
                    continue
                assert handle is not None
                task_id = handle.task_id
                if not replayed:
                    await status_reactor.running(msg)

                typing_task = None if replayed else _start_typing_keepalive(channel, msg)

                async def _reply_task_body(
                    _channel: Any = channel,
                    _task_runtime: Any = task_runtime,
                    _session_manager: Any = session_manager,
                    _session_key: str = session_key,
                    _task_id: str = task_id,
                    _route_envelope: Any = route_envelope,
                    _inbound: Any = msg,
                    _transcript_watermark: int = transcript_watermark,
                    _replayed: bool = replayed,
                    _stream_relay: Any = stream_relay,
                    _typing_task: Any = typing_task,
                    _event_bridge: Any = event_bridge,
                    _status_reactor: Any = status_reactor,
                ) -> None:
                    try:
                        await _deliver_runtime_channel_reply(
                            channel=_channel,
                            task_runtime=_task_runtime,
                            session_manager=_session_manager,
                            session_key=_session_key,
                            task_id=_task_id,
                            route_envelope=_route_envelope,
                            inbound=_inbound,
                            transcript_watermark=_transcript_watermark,
                            replayed=_replayed,
                            config=config,
                            stream_relay=_stream_relay,
                        )
                    finally:
                        if _typing_task is not None:
                            _typing_task.cancel()
                        if _event_bridge is not None:
                            await _emit_events(
                                _event_bridge,
                                _session_key,
                                "turn_complete",
                            )
                        await _status_reactor.completed(_inbound)

                reply_task = asyncio.create_task(
                    _reply_task_body(),
                    name=f"channel_reply:{session_key}",
                )
                _in_flight.add(reply_task)

                def _reply_done(t: asyncio.Task[Any], _sk: str = session_key) -> None:
                    _in_flight.discard(t)
                    exc = t.exception() if not t.cancelled() else None
                    if exc is not None:
                        log.error(
                            "channel_dispatch.reply_task_error",
                            session_key=_sk,
                            error_type=type(exc).__name__,
                            error=str(exc),
                            exc_info=exc,
                        )
                        _emit_metric(
                            "turn_cancellations_total",
                            value=1,
                            reason="reply_task_error",
                            session_key=_sk,
                        )

                reply_task.add_done_callback(_reply_done)
                if delivery_store is not None:
                    delivery_store.complete_inbound(
                        ingress_claim, "turn_dispatched", reason=admission.reason
                    )
            continue

        # Gap 3: Start typing indicator (background task)
        typing_task = _start_typing_keepalive(channel, msg)
        try:
            # Gap 4: Run agent turn with streaming (or batch fallback)
            assert ingested is not None
            await _run_turn_with_streaming(
                channel,
                turn_runner,
                msg,
                session_key,
                event_bridge,
                semantic_message=raw_content,
                config=config,
                route_envelope=route_envelope,
                attachments=ingested.attachments,
                session_manager=session_manager,
            )
        except BaseException as exc:
            if delivery_store is not None:
                delivery_store.fail_inbound(ingress_claim, exc)
            raise
        finally:
            if typing_task is not None:
                typing_task.cancel()

        # Gap 5: Emit turn-complete event
        if event_bridge is not None:
            await _emit_events(
                event_bridge,
                session_key,
                "turn_complete",
            )
        if delivery_store is not None:
            delivery_store.complete_inbound(
                ingress_claim, "turn_completed", reason=admission.reason
            )


def _slash_command_head(content: str) -> str | None:
    stripped = content.strip()
    if not stripped or not stripped.startswith("/") or stripped in {"/", "//"}:
        return None
    if stripped.startswith("//"):
        return None
    return stripped.split(maxsplit=1)[0]


# Failed approval-code attempts per (session, sender). Live-but-unauthorized
# codes already get the same reply as unknown ones; this budget additionally
# caps how fast an admitted sender can enumerate the code space at all.
_APPROVAL_PROBE_WINDOW_S = 60.0
_APPROVAL_PROBE_LIMIT = 5
_approval_probe_failures: dict[str, list[float]] = {}


def _reset_approval_probe_throttle() -> None:
    """Clear recorded probe failures (test helper)."""
    _approval_probe_failures.clear()


def _approval_probe_key(session_key: str, sender_id: str) -> str:
    return f"{session_key}\x00{sender_id}"


def _approval_probe_throttled(probe_key: str) -> bool:
    now = time.monotonic()
    attempts = [
        t for t in _approval_probe_failures.get(probe_key, ()) if now - t < _APPROVAL_PROBE_WINDOW_S
    ]
    if attempts:
        _approval_probe_failures[probe_key] = attempts
    else:
        _approval_probe_failures.pop(probe_key, None)
    return len(attempts) >= _APPROVAL_PROBE_LIMIT


def _record_approval_probe_failure(probe_key: str) -> None:
    now = time.monotonic()
    _approval_probe_failures.setdefault(probe_key, []).append(now)
    # Opportunistic global prune so abandoned senders cannot grow the map
    # without bound.
    if len(_approval_probe_failures) > 1024:
        for key in list(_approval_probe_failures):
            kept = [t for t in _approval_probe_failures[key] if now - t < _APPROVAL_PROBE_WINDOW_S]
            if kept:
                _approval_probe_failures[key] = kept
            else:
                _approval_probe_failures.pop(key, None)


class _SandboxChoiceError(Exception):
    """Sandbox choice validation failed before any queue state changed."""


async def _maybe_resolve_channel_approval(
    *,
    msg: IncomingMessage,
    session_key: str,
    config: Any = None,
    session_manager: Any = None,
    route_envelope: Any = None,
) -> OutgoingMessage | None:
    """Resolve a channel approval action without starting an agent turn.

    Recognises a Feishu ``approval_resolve`` card action or the universal
    ``/approve <code>`` / ``/deny <code>`` / ``/approve <code> always`` text
    command, then resolves the bound approval so the suspended tool call's
    ``wait()`` unblocks. Sandbox-kind approvals go through the same
    claim/finalize/apply-choice sequence as the Web UI resolver so their
    choice semantics (durable same-type grants) actually take effect.

    Security: only the session owner (the ``sender_id`` that started the
    originating turn, recorded on the approval at request time) may resolve.
    Any other sender is rejected without resolving. Cross-session and
    cross-chat attempts get the same reply as an unknown code — response text
    must not become a short-code existence oracle — and repeated failed
    attempts per sender hit a cooldown. The ``always`` decision additionally
    requires the sender to carry the authenticated channel-admin stamp from
    ingress — the card payload and raw sender ID are never trusted for that.
    A plain approval
    forces ``elevated_mode=None`` so it permits one gated command, never
    session-wide elevation. Returns the reply to send, or ``None`` when the
    message is not an approval action.
    """
    from opensquilla.channels.approval_prompt import (
        DECISION_ALWAYS,
        DECISION_DENY,
        parse_approval_action,
        resolve_short_code,
    )

    parsed = parse_approval_action(msg)
    if parsed is None:
        return None
    code, decision = parsed
    approved = decision != DECISION_DENY

    provenance = getattr(msg, "provenance", None)
    principal = getattr(provenance, "principal", None)
    authenticated = bool(getattr(provenance, "authenticated", False))
    sender_id = (
        str(getattr(principal, "subject_id", "") or "").strip()
        if authenticated
        else (msg.sender_id or "").strip()
    )
    probe_key = _approval_probe_key(session_key, sender_id)
    if _approval_probe_throttled(probe_key):
        log.warning(
            "channel.approval_probe_throttled",
            session_key=session_key,
            sender_id=sender_id,
        )
        # Constant reply regardless of the attempted code: a throttled probe
        # must learn nothing about code validity.
        return OutgoingMessage(
            content=render_channel_message("approval_probe_throttled", config=config)
        )

    binding = resolve_short_code(code)
    if binding is None:
        log.info("channel.approval_unknown_code", code=code, session_key=session_key)
        _record_approval_probe_failure(probe_key)
        return OutgoingMessage(
            content=render_channel_message("approval_no_pending", config=config, code=code)
        )

    # Cross-session and cross-chat attempts reuse the unknown-code reply on
    # purpose: distinct texts would confirm that a guessed code is live and
    # leak where it originates.
    if binding.session_key and binding.session_key != session_key:
        log.warning(
            "channel.approval_session_mismatch",
            code=code,
            session_key=session_key,
        )
        _record_approval_probe_failure(probe_key)
        return OutgoingMessage(
            content=render_channel_message("approval_no_pending", config=config, code=code)
        )

    if binding.origin_channel_id and msg.channel_id != binding.origin_channel_id:
        log.warning(
            "channel.approval_origin_mismatch",
            code=code,
            session_key=session_key,
            channel_id=msg.channel_id,
        )
        _record_approval_probe_failure(probe_key)
        return OutgoingMessage(
            content=render_channel_message("approval_no_pending", config=config, code=code)
        )

    if not sender_id or sender_id != binding.owner_sender_id:
        # Reaching this check requires matching the binding's session AND
        # origin chat — where the prompt already displays the code — so the
        # helpful reply reveals nothing that chat cannot already see.
        log.warning(
            "channel.approval_owner_mismatch",
            code=code,
            session_key=session_key,
            sender_id=sender_id,
        )
        _record_approval_probe_failure(probe_key)
        return OutgoingMessage(
            content=render_channel_message(
                "approval_owner_only",
                config=config,
                code=code,
            )
        )

    if decision == DECISION_ALWAYS and not has_verified_channel_admin_stamp(route_envelope):
        log.warning(
            "channel.approval_always_requires_admin",
            code=code,
            session_key=session_key,
            sender_id=sender_id,
        )
        return OutgoingMessage(
            content=render_channel_message(
                "approval_always_requires_admin",
                config=config,
                code=code,
            )
        )

    from opensquilla.gateway.approval_queue import get_approval_queue

    queue = get_approval_queue()
    try:
        reply = await _resolve_channel_approval_decision(
            queue,
            approval_id=binding.approval_id,
            code=code,
            decision=decision,
            approved=approved,
            config=config,
            session_manager=session_manager,
        )
    except KeyError:
        log.info("channel.approval_expired", code=code, session_key=session_key)
        return OutgoingMessage(
            content=render_channel_message("approval_no_pending", config=config, code=code)
        )
    except _SandboxChoiceError as exc:
        # Malformed/incompatible choice payload — nothing was claimed, the
        # approval is genuinely still pending (distinct from the resolved
        # race below, which must stay idempotent).
        log.warning(
            "channel.approval_choice_invalid",
            code=code,
            session_key=session_key,
            error=str(exc),
        )
        return OutgoingMessage(
            content=render_channel_message("approval_invalid_choice", config=config, code=code)
        )
    except ValueError:
        # Already resolved (race) — report idempotently rather than erroring.
        log.info("channel.approval_already_resolved", code=code, session_key=session_key)
        return OutgoingMessage(
            content=render_channel_message(
                "approval_already_resolved", config=config, code=code
            )
        )
    except Exception:
        # Transient storage/session failures (e.g. a busy SQLite write while
        # applying a grant) must not escape into the dispatch loop and burn
        # the channel's restart budget. The decision helper reopens/releases
        # queue state on failure, so the approval is still pending.
        log.exception(
            "channel.approval_resolution_failed",
            code=code,
            session_key=session_key,
        )
        return OutgoingMessage(
            content=render_channel_message(
                "approval_resolution_failed",
                config=config,
                code=code,
            )
        )

    log.info(
        "channel.approval_resolved",
        code=code,
        approved=approved,
        always=decision == DECISION_ALWAYS,
        session_key=session_key,
        sender_id=sender_id,
    )
    return reply


def _sender_is_channel_admin(config: Any, channel_name: str, sender_id: str) -> bool:
    admin_senders = getattr(config, "channel_admin_senders", None)
    if not isinstance(admin_senders, dict) or not channel_name or not sender_id:
        return False
    return sender_is_channel_admin(sender_id, configured=admin_senders.get(channel_name))


async def _resolve_channel_approval_decision(
    queue: Any,
    *,
    approval_id: str,
    code: str,
    decision: str,
    approved: bool,
    config: Any,
    session_manager: Any,
) -> OutgoingMessage:
    """Apply one parsed approval decision to the queue.

    Sandbox-kind approvals replay the Web UI resolver's claim → finalize →
    apply-choice → complete sequence so ``always`` (``allow_same_type``)
    produces its durable grant; everything else keeps the original single
    ``resolve()``. Raises ``KeyError``/``ValueError`` exactly like
    ``queue.resolve`` so the caller's reply mapping stays unchanged.
    """
    from opensquilla.channels.approval_prompt import DECISION_ALWAYS
    from opensquilla.sandbox.escalation import (
        apply_sandbox_approval_choice,
        deny_matching_pending_sandbox_approvals,
        is_sandbox_approval_kind,
        remember_sandbox_approval_denial,
        validate_sandbox_approval_choice,
    )

    pending = queue.get(approval_id)
    sandbox_approval = is_sandbox_approval_kind(pending.params.get("approvalKind"))
    choice: str | None = None
    if sandbox_approval and approved:
        # Sandbox kinds refuse empty choices, so a plain Approve must select
        # the primary one-shot choice explicitly; "always" selects the
        # durable same-type grant.
        choice = "allow_same_type" if decision == DECISION_ALWAYS else "allow_once"
        try:
            validate_sandbox_approval_choice(pending.params, choice=choice, approved=True)
        except ValueError as exc:
            # Nothing has been claimed yet — surface this as a validation
            # failure, NOT as the caller's already-resolved ValueError race.
            raise _SandboxChoiceError(str(exc)) from exc
        claim_token = queue.claim_resolution(
            approval_id,
            resolution_metadata={"resolutionSource": "user_channel"},
        )
        pending = queue.get(approval_id)
        try:
            queue.finalize_claimed_resolution(
                approval_id,
                claim_token,
                True,
                elevated_mode=None,
            )
        except Exception:
            queue.release_resolution_claim(approval_id, claim_token)
            raise
        try:
            await apply_sandbox_approval_choice(
                pending.params,
                approval_id=approval_id,
                choice=choice,
                approved=True,
                session_manager=session_manager,
                config=config,
            )
        except Exception:
            queue.reopen_resolved_approval(approval_id, expected_approved=True)
            raise
        queue.complete_claimed_resolution(approval_id, claim_token)
    else:
        # Force elevated_mode=None: a channel approval permits exactly the one
        # gated command, never a session-wide bypass regardless of payload.
        queue.resolve(
            approval_id,
            approved,
            elevated_mode=None,
            allow_idempotent=not sandbox_approval,
            resolution_metadata={"resolutionSource": "user_channel"},
        )
        if sandbox_approval and not approved:
            remember_sandbox_approval_denial(pending.params, approval_id)
            deny_matching_pending_sandbox_approvals(
                queue,
                pending.params,
                exclude_approval_id=approval_id,
            )

    if not approved:
        return OutgoingMessage(
            content=render_channel_message("approval_denied", config=config, code=code)
        )
    if choice == "allow_same_type":
        return OutgoingMessage(
            content=render_channel_message(
                "approval_approved_always",
                config=config,
                code=code,
            )
        )
    return OutgoingMessage(
        content=render_channel_message(
            "approval_approved_once",
            config=config,
            code=code,
        )
    )


async def _dispatch_channel_slash_command(
    *,
    route_envelope: Any,
    msg: IncomingMessage,
    session_manager: Any,
    session_key: str,
    session_prefix: str,
    rpc_dispatcher: Any,
    context_factory: Callable[[Any], Any],
    config: Any = None,
) -> OutgoingMessage | None:
    from opensquilla.channels.command_registry import DEFAULT_COMMAND_REGISTRY

    match = DEFAULT_COMMAND_REGISTRY.match(route_envelope, msg.content)
    if match is None:
        head = _slash_command_head(msg.content)
        if head is None:
            return None
        return _route_envelope_reply_message(
            render_channel_message("command_unsupported", config=config, command=head),
            route_envelope,
            metadata={"command": head[1:].lower(), "method": None, "unsupported": True},
        )

    name, method, _params_factory = match
    if name == "new" and method == "sessions.reset":
        return await _dispatch_channel_new_command(
            route_envelope=route_envelope,
            msg=msg,
            session_manager=session_manager,
            session_key=session_key,
            session_prefix=session_prefix,
            rpc_dispatcher=rpc_dispatcher,
            context_factory=context_factory,
            config=config,
        )

    reply = await DEFAULT_COMMAND_REGISTRY.dispatch(
        envelope=route_envelope,
        message_content=msg.content,
        rpc_dispatcher=rpc_dispatcher,
        context_factory=context_factory,
        config=config,
    )
    if reply is None:
        return None
    return _preserve_route_channel_metadata(reply, route_envelope)


async def _dispatch_channel_new_command(
    *,
    route_envelope: Any,
    msg: IncomingMessage,
    session_manager: Any,
    session_key: str,
    session_prefix: str,
    rpc_dispatcher: Any,
    context_factory: Callable[[Any], Any],
    config: Any = None,
) -> OutgoingMessage:
    from opensquilla.channels.command_registry import DEFAULT_COMMAND_REGISTRY
    from opensquilla.gateway.scopes import WRITE_SCOPE, authorize_call

    ctx = context_factory(route_envelope)
    principal = getattr(ctx, "principal", None)
    allowed, missing = authorize_call(
        "sessions.reset",
        WRITE_SCOPE,
        getattr(principal, "role", ""),
        getattr(principal, "scopes", frozenset()),
    )
    if not allowed:
        detail = (
            render_channel_message("command_missing_scope", config=config, missing=missing)
            if missing
            else ""
        )
        return _route_envelope_reply_message(
            render_channel_message(
                "command_new_denied",
                config=config,
                method="sessions.reset",
                detail=detail,
            ),
            route_envelope,
            metadata={"command": "new", "method": "sessions.reset", "denied": True},
        )

    await _record_delivery_context(
        session_manager,
        session_key,
        msg,
        session_prefix,
        route_envelope=route_envelope,
    )
    reply = await DEFAULT_COMMAND_REGISTRY.dispatch(
        envelope=route_envelope,
        message_content=msg.content,
        rpc_dispatcher=rpc_dispatcher,
        context_factory=lambda _envelope: ctx,
        config=config,
    )
    if reply is None:
        return _route_envelope_reply_message(
            render_channel_message("command_new_unavailable", config=config),
            route_envelope,
            metadata={"command": "new", "method": "sessions.reset", "denied": False},
        )
    return _preserve_route_channel_metadata(reply, route_envelope)


# fmt: off
async def _dispatch_combined_message_after_debounce(channel: Any, combined: Any, turn_runner: Any, session_manager: Any, session_key: str, session_prefix: str, task_runtime: Any, config: Any = None, event_bridge: EventBridge | None = None, _in_flight: _ChannelInFlightSet | None = None, channel_rpc_context_factory: Callable[[Any], Any] | None = None, admission_decision: ChannelAdmissionDecision | None = None, busy_input_mode: str = "followup") -> None:  # noqa: E501
    from opensquilla.gateway.routing import build_channel_route_envelope

    msg = combined.message
    admission = admission_decision or decide_channel_admission(
        channel,
        msg,
        session_key,
        config=config,
        channel_name=session_prefix,
    )
    if not admission.admit:
        log.info("channel.admission_denied", channel=session_prefix, reason=admission.reason, is_group=admission.is_group)  # noqa: E501
        return
    route_envelope = build_channel_route_envelope(msg, session_key=session_key, session_prefix=session_prefix)  # noqa: E501
    principal_is_owner = _stamp_channel_admin_principal(config, route_envelope, msg)
    approval_reply = await _maybe_resolve_channel_approval(
        msg=msg,
        session_key=session_key,
        config=config,
        session_manager=session_manager,
        route_envelope=route_envelope,
    )
    if approval_reply is not None:
        await channel.send(_preserve_route_channel_metadata(approval_reply, route_envelope))
        return
    _get_lock = getattr(turn_runner, "_get_session_lock", None)
    session_lock = _get_lock(session_key) if callable(_get_lock) else None
    atomic_channel_acceptance = _supports_atomic_channel_acceptance(
        session_manager,
        task_runtime,
    )
    async with _maybe_lock(session_lock):
        # Mention gating already ran via the admission decision at the top of
        # this function; denied messages never reach this point.
        if not atomic_channel_acceptance:
            await _record_delivery_context(session_manager, session_key, msg, session_prefix, route_envelope=route_envelope)  # noqa: E501

    ingested: AttachmentIngestResult | None = None
    if not atomic_channel_acceptance:
        await _apply_saved_channel_run_context(
            route_envelope,
            session_manager=session_manager,
            config=config,
            workspace_dir=None,
            principal_is_owner=principal_is_owner,
        )

        ingested = await _ingest_channel_message_attachments(
            channel=channel,
            msg=msg,
            config=config,
        )

    if not atomic_channel_acceptance:
        async with _maybe_lock(session_lock):
            await _record_delivery_context(session_manager, session_key, msg, session_prefix, route_envelope=route_envelope)  # noqa: E501

    status_reactor = _status_reactor(channel)
    await status_reactor.received(msg)
    raw_content = getattr(combined, "raw_content", None) or msg.content
    from opensquilla.gateway.task_runtime import TaskQueueFullError

    # Cap check BEFORE enqueue/append: reject early so no transcript entry is
    # written and no runtime turn is started (accept-then-drop fix).
    # try_acquire atomically checks + reserves a slot so that two concurrent
    # debounce callbacks racing through this path cannot both pass the guard.
    _reservation_token = object()
    if _in_flight is not None:
        if not _in_flight.try_acquire(_reservation_token):
            _emit_metric(
                "queue_full_errors_total",
                value=1,
                session_key=session_key,
            )
            log.warning(
                "channel_dispatch.inflight_cap_reached",
                session_key=session_key,
                cap=_in_flight.cap,
            )
            await channel.send(
                _route_envelope_reply_message(
                    "Server busy, please retry",
                    route_envelope,
                )
            )
            await status_reactor.completed(msg)
            return
    else:
        _reservation_token = None  # type: ignore[assignment]

    transcript_watermark = await _transcript_watermark(session_manager, session_key)
    stream_relay = None
    replayed = False
    try:
        async with _channel_acceptance_lock(
            session_lock,
            atomic=atomic_channel_acceptance,
        ):
            if atomic_channel_acceptance:
                handle, persisted_content, stream_relay, replayed = await _accept_channel_runtime_turn(  # noqa: E501
                    channel=channel,
                    msg=msg,
                    session_manager=session_manager,
                    session_key=session_key,
                    route_envelope=route_envelope,
                    task_runtime=task_runtime,
                    ingested=ingested,
                    raw_content=raw_content,
                    config=config,
                    principal_is_owner=principal_is_owner,
                    busy_input_mode=busy_input_mode,
                )
            else:
                assert ingested is not None
                stream_relay = _RuntimeChannelStreamRelay.maybe_start(channel, msg, task_runtime, config)  # noqa: E501
                channel_overflow_policy = _resolve_channel_overflow_policy(channel, config)
                if channel_overflow_policy is not None:
                    apply_policy = getattr(task_runtime, "apply_overflow_policy", None)
                    if callable(apply_policy):
                        await apply_policy(session_key, policy=channel_overflow_policy)
                handle = await start_turn_via_runtime(task_runtime, route_envelope, msg.content, attachments=ingested.attachments, mode=_resolve_channel_busy_input_mode(task_runtime, busy_input_mode), run_kind="channel_turn", semantic_message=raw_content, stream_event_sink=stream_relay.emit if stream_relay is not None else None)  # noqa: E501
                _persisted, persisted_content = await _append_channel_user_message(
                    session_manager=session_manager,
                    session_key=session_key,
                    text=ingested.text,
                    attachments=ingested.attachments,
                    config=config,
                )
            msg.content = persisted_content
    except Exception as exc:
        if _in_flight is not None and _reservation_token is not None:
            _in_flight.release(_reservation_token)
        if stream_relay is not None:
            await stream_relay.close()

        from opensquilla.project_workspaces import ProjectWorkspaceStateError
        from opensquilla.session.storage import (
            StaleEpochError,
            StorageBusyError,
            TurnIngressConflictError,
        )

        if isinstance(exc, StorageBusyError):
            await status_reactor.failed(msg)
            await channel.send(_route_envelope_reply_message("Session storage is busy. Please retry these messages.", route_envelope))  # noqa: E501
            return
        if isinstance(exc, StaleEpochError):
            await status_reactor.failed(msg)
            await channel.send(_route_envelope_reply_message("The session changed while accepting these messages. Please retry.", route_envelope))  # noqa: E501
            return
        if isinstance(exc, TurnIngressConflictError):
            await status_reactor.failed(msg)
            log.warning("channel.ingress_idempotency_conflict", session_key=session_key)
            await channel.send(_route_envelope_reply_message("This channel message id was already used; the duplicate was ignored.", route_envelope))  # noqa: E501
            return
        if isinstance(exc, ProjectWorkspaceStateError):
            await status_reactor.failed(msg)
            workspace_message = (
                "Project workspace not found. Restore it and retry."
                if exc.reason in {"not_found", "removed"}
                else "The project workspace is unavailable. Restore access and retry."
            )
            await channel.send(
                _route_envelope_reply_message(
                    workspace_message,
                    route_envelope,
                )
            )
            return
        if isinstance(exc, TaskQueueFullError):
            await status_reactor.failed(msg)
            log.warning("channel_dispatch.debounce_enqueue_failed", session_key=session_key, reason="queue_full", coalesced_count=combined.coalesced_count)  # noqa: E501
            await channel.send(_route_envelope_reply_message("Your messages couldn't be processed because the queue is full. Please retry.", route_envelope))  # noqa: E501
            return
        log.exception("channel_dispatch.debounce_enqueue_failed", session_key=session_key, reason="unexpected")  # noqa: E501
        await status_reactor.failed(msg)
        return

    if replayed and handle is None:
        if _in_flight is not None and _reservation_token is not None:
            _in_flight.release(_reservation_token)
        await status_reactor.completed(msg)
        return
    assert handle is not None

    # Enqueue succeeded — release the placeholder reservation now that the real
    # reply delivery will proceed (it doesn't use _in_flight in this path).
    if _in_flight is not None and _reservation_token is not None:
        _in_flight.release(_reservation_token)

    if not replayed:
        await status_reactor.running(msg)
    typing_task = None if replayed else _start_typing_keepalive(channel, msg)
    try:
        await _deliver_runtime_channel_reply(channel=channel, task_runtime=task_runtime, session_manager=session_manager, session_key=session_key, task_id=handle.task_id, route_envelope=route_envelope, inbound=msg, transcript_watermark=transcript_watermark, replayed=replayed, config=config, stream_relay=stream_relay)  # noqa: E501
    finally:
        if typing_task is not None:
            typing_task.cancel()
    if event_bridge is not None:
        await _emit_events(event_bridge, session_key, "turn_complete")
    await status_reactor.completed(msg)
# fmt: on


# ── Gap 1: Delivery context ─────────────────────────────────────────────


async def _record_delivery_context(
    session_manager: Any,
    session_key: str,
    msg: IncomingMessage,
    session_prefix: str,
    route_envelope: Any = None,
) -> tuple[Any, bool]:
    """Ensure session exists and record delivery routing fields.

    On first message (created=True), fields are set at creation time.
    On subsequent messages, fields are updated via session_manager.update().
    Returns (session, created).
    """
    from opensquilla.gateway.routing import (
        build_channel_route_envelope,
        delivery_fields_from_envelope,
    )

    envelope = route_envelope or build_channel_route_envelope(
        msg,
        session_key=session_key,
        session_prefix=session_prefix,
    )
    delivery_fields = delivery_fields_from_envelope(envelope)

    from opensquilla.session.keys import build_main_key, parse_agent_id

    agent_id = parse_agent_id(session_key)
    main_session_key = build_main_key(agent_id)

    session, created = await session_manager.get_or_create(
        session_key,
        agent_id=agent_id,
        **delivery_fields,
    )

    if not created:
        await session_manager.update(session_key, **delivery_fields)

    if main_session_key != session_key:
        _main_session, main_created = await session_manager.get_or_create(
            main_session_key,
            agent_id=agent_id,
            **delivery_fields,
        )
        if not main_created:
            await session_manager.update(main_session_key, **delivery_fields)

    return session, created


async def _apply_saved_channel_run_context(
    route_envelope: Any,
    *,
    session_manager: Any,
    config: Any,
    workspace_dir: str | None,
    principal_is_owner: bool,
) -> None:
    """Attach the effective saved or global sandbox context to a channel route."""
    if route_envelope is None or session_manager is None or config is None:
        return
    try:
        from opensquilla.gateway.rpc_sessions import _apply_run_context_route_metadata
        from opensquilla.sandbox.run_context import get_run_context

        run_context = await get_run_context(
            session_manager,
            route_envelope.session_key,
            config=config,
            workspace=workspace_dir,
        )
    except KeyError:
        # First channel message: the atomic acceptance path has intentionally
        # not created the session yet, so no saved context can exist.
        return
    except Exception as exc:  # pragma: no cover - defensive channel path
        log.warning(
            "channel_dispatch.run_context_load_failed",
            session_key=getattr(route_envelope, "session_key", ""),
            error_type=type(exc).__name__,
        )
        return
    _apply_run_context_route_metadata(
        route_envelope,
        run_context,
        principal_is_owner=principal_is_owner,
    )


async def resolve_delivery_target(
    session_manager: Any,
    session_key: str,
) -> dict[str, Any] | None:
    """Read delivery routing from a session for outbound use (e.g. cron).

    Returns ``{"channel": ..., "to": ..., "thread_id": ...}`` or None
    if the session has no delivery context.
    """
    try:
        node = await session_manager.resume(session_key)
    except KeyError:
        return None

    if not node.last_channel:
        return None

    return {
        "channel": node.last_channel,
        "to": node.last_to,
        "account_id": node.last_account_id,
        "thread_id": node.last_thread_id,
        "delivery_context": node.delivery_context,
    }


# ── Gap 2: Authenticated admission / mention gating ─────────────────────


def _should_skip_unmentioned(
    channel: Any,
    msg: IncomingMessage,
    session_key: str,
) -> bool:
    """Compatibility wrapper around the shared pre-dispatch admission decision."""

    return not decide_channel_admission(channel, msg, session_key).admit


# ── Gap 3: Typing indicator ──────────────────────────────────────────────


def _start_typing_keepalive(
    channel: Any,
    inbound: IncomingMessage | None = None,
    interval: float = 8.0,
) -> asyncio.Task | None:
    """Start a background task that re-sends typing every ``interval`` seconds.

    Uses ``asyncio.create_task`` so typing continues even during long tool calls
    where no events are yielded (a timestamp-in-loop approach would fail here).

    Returns None if the adapter has no ``send_typing`` method (e.g. Feishu, Terminal).
    The caller MUST cancel the returned task in a ``finally`` block.
    """
    if not resolve_channel_stream_policy(channel).typing_keepalive:
        return None
    send_typing = getattr(channel, "send_typing", None)
    if not callable(send_typing):
        return None

    async def _keepalive() -> None:
        while True:
            try:
                if inbound is not None and _accepts_keyword_arg(send_typing, "channel_id"):
                    await send_typing(channel_id=inbound.channel_id)
                else:
                    await send_typing()
            except Exception:
                pass  # typing is best-effort, never crash the loop
            await asyncio.sleep(interval)

    return asyncio.create_task(_keepalive())


# ── Gap 4: Streaming / batch turn execution ──────────────────────────────


def _optional_positive_config_float(config: Any, attr: str, default: float) -> float | None:
    raw = getattr(config, attr, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return value if value > 0 else None


def _wrap_channel_turn_stream(
    stream: Any,
    config: Any,
    *,
    context_bound: bool | None = None,
) -> Any:
    from opensquilla.engine.stream_wrappers import wrap_stream

    raw_stream_idle_timeout = effective_agent_stream_idle_timeout_seconds(config)
    stream_idle_timeout: float | None = (
        raw_stream_idle_timeout if raw_stream_idle_timeout > 0 else None
    )
    return wrap_stream(
        stream,
        idle_timeout=stream_idle_timeout,
        heartbeat_interval=_optional_positive_config_float(
            config,
            "agent_stream_heartbeat_interval_seconds",
            _DEFAULT_STREAM_HEARTBEAT_INTERVAL_SECONDS,
        ),
        heartbeat_phase="channel",
        heartbeat_message="Still working",
        context_bound=context_bound,
    )


async def _emit_run_heartbeat(
    event_bridge: EventBridge | None,
    session_key: str,
    event: RunHeartbeatEvent,
) -> None:
    if event_bridge is None:
        return
    await event_bridge.emit(
        session_key,
        "session.event.run_heartbeat",
        {
            "phase": event.phase,
            "elapsed_ms": event.elapsed_ms,
            "idle_ms": event.idle_ms,
            "message": event.message,
        },
    )


def _is_channel_admin_sender(config: Any, envelope: Any) -> bool:
    """Compatibility matcher for callers that only have a route envelope.

    This helper intentionally does not authorize a turn.  Channel dispatch
    uses ``_stamp_channel_admin_principal`` below, which also proves the
    authenticated ingress principal.
    """

    source_name = getattr(envelope, "source_name", None)
    sender_id = getattr(envelope, "sender_id", None)
    if not isinstance(source_name, str) or not isinstance(sender_id, str):
        return False
    return _sender_is_channel_admin(config, source_name, sender_id)


def _stamp_channel_admin_principal(
    config: Any,
    envelope: Any,
    msg: IncomingMessage,
) -> bool:
    """Persist authenticated channel-admin standing on an accepted route.

    TaskRuntime runs after ingress has returned, so it cannot consult the
    mutable channel configuration safely or infer ownership from a session key.
    Record only the result of the authenticated ingress check here; raw
    adapter metadata and a configured sender ID alone are never authorization
    sources.
    """
    source_kind = getattr(envelope, "source_kind", None)
    source_kind_value = getattr(source_kind, "value", source_kind)
    source_name = getattr(envelope, "source_name", None)
    route_sender_id = getattr(envelope, "sender_id", None)
    is_owner = bool(
        source_kind_value == "channel"
        and route_sender_id == msg.sender_id
        and is_authenticated_channel_admin(
            config,
            channel_name=source_name if isinstance(source_name, str) else None,
            msg=msg,
        )
    )
    metadata = getattr(envelope, "metadata", None)
    if isinstance(metadata, dict):
        metadata["principal_is_owner"] = is_owner
        metadata[CHANNEL_ADMIN_VERIFIED_METADATA_KEY] = is_owner
    return is_owner


async def _run_turn_with_streaming(
    channel: Any,
    turn_runner: Any,
    msg: IncomingMessage,
    session_key: str,
    event_bridge: EventBridge | None = None,
    semantic_message: str | None = None,
    config: Any = None,
    route_envelope: Any = None,
    attachments: list[dict[str, Any]] | None = None,
    session_manager: Any = None,
) -> None:
    """Run the agent turn, sending reply via streaming or batch.

    If the adapter has ``send_streaming``, text deltas are fed through
    an async iterator that the adapter consumes (post + throttled edits).
    Otherwise falls back to batch mode (accumulate all text, send once).

    Error recovery: if an ErrorEvent occurs mid-stream, the existing
    message is edited to append "(Error: ...)" rather than leaving partial
    text visible.  Pre-stream errors send a standalone error message.
    """
    from opensquilla.agents.scope import resolve_agent_workspace_dir
    from opensquilla.gateway.project_workspace_runtime import (
        authoritative_project_run_context,
    )
    from opensquilla.gateway.routing import build_channel_route_envelope, tool_context_from_envelope
    from opensquilla.gateway.session_services import get_session_storage
    from opensquilla.session.keys import parse_agent_id

    agent_id = parse_agent_id(session_key)
    workspace_dir: Path | str | None = resolve_agent_workspace_dir(agent_id, config)
    workspace_strict = getattr(config, "workspace_strict", None)
    if not isinstance(workspace_strict, bool):
        workspace_strict = bool(workspace_dir)
    envelope = route_envelope or build_channel_route_envelope(
        msg,
        session_key=session_key,
        session_prefix=getattr(channel, "channel_id", None) or "unknown",
        agent_id=agent_id,
    )
    principal_is_owner = _stamp_channel_admin_principal(config, envelope, msg)
    storage = get_session_storage(session_manager)
    if storage is not None:
        session = await storage.get_session(session_key)
        if session is None:
            raise KeyError(f"Session not found: {session_key}")
        run_context, workspace_guard = await authoritative_project_run_context(
            storage=storage,
            session_manager=session_manager,
            session=session,
            config=config,
            default_workspace=(str(workspace_dir) if workspace_dir is not None else None),
        )
        from opensquilla.gateway.rpc_sessions import (
            _apply_run_context_route_metadata,
        )

        _apply_run_context_route_metadata(
            envelope,
            run_context,
            principal_is_owner=principal_is_owner,
        )
        if run_context.workspace is not None:
            workspace_dir = run_context.workspace
        if workspace_guard is not None and not isinstance(
            getattr(config, "workspace_strict", None),
            bool,
        ):
            workspace_strict = True
    tool_ctx = tool_context_from_envelope(
        envelope,
        is_owner=principal_is_owner,
        workspace_dir=(str(workspace_dir) if workspace_dir is not None else None),
        workspace_strict=workspace_strict,
        default_elevated=configured_default_elevated(config),
    )
    from opensquilla.sandbox.policy_store import pin_sandbox_policy

    pin_sandbox_policy(tool_ctx, config)
    use_streaming = resolve_channel_stream_policy(channel).relay_stream

    if use_streaming:
        await _run_turn_streaming_path(
            channel,
            turn_runner,
            msg,
            session_key,
            tool_ctx,
            event_bridge,
            semantic_message,
            config,
            attachments,
        )
    else:
        await _run_turn_batch_path(
            channel,
            turn_runner,
            msg,
            session_key,
            tool_ctx,
            event_bridge,
            semantic_message,
            config,
            attachments,
        )


def _build_reply_message(channel: Any, content: str, msg: IncomingMessage) -> OutgoingMessage:
    builder = getattr(channel, "build_reply_message", None)
    if callable(builder):
        reply = builder(content, msg)
        if isinstance(reply, OutgoingMessage):
            return _sanitize_outgoing_message(reply)
    return _sanitize_outgoing_message(OutgoingMessage(content=content))


def _route_envelope_reply_message(
    content: str,
    route_envelope: Any,
    *,
    metadata: dict[str, Any] | None = None,
) -> OutgoingMessage:
    """Build a reply that preserves channel id when targeting a thread id."""
    channel_id = getattr(route_envelope, "channel_id", None)
    thread_id = getattr(route_envelope, "thread_id", None)
    merged_metadata = _merge_route_reply_metadata(metadata, route_envelope)
    if thread_id and channel_id:
        merged_metadata.setdefault("channel", channel_id)
    return _sanitize_outgoing_message(
        OutgoingMessage(
            content=content,
            reply_to=thread_id or channel_id,
            metadata=merged_metadata,
        )
    )


_INTERACTION_REPLY_METADATA_KEYS = (
    "interaction_token",
    "application_id",
    "interaction_deferred",
)


def _merge_route_reply_metadata(
    metadata: dict[str, Any] | None,
    route_envelope: Any,
) -> dict[str, Any]:
    """Merge only provider callback fields that are safe for an interaction reply."""

    merged = dict(metadata or {})
    route_metadata = getattr(route_envelope, "metadata", None)
    if not isinstance(route_metadata, dict):
        return merged
    for key in _INTERACTION_REPLY_METADATA_KEYS:
        value = route_metadata.get(key)
        if key == "interaction_deferred":
            if isinstance(value, bool):
                merged.setdefault(key, value)
        elif isinstance(value, str) and value:
            merged.setdefault(key, value)
    return merged


def _preserve_route_channel_metadata(
    reply: OutgoingMessage,
    route_envelope: Any,
) -> OutgoingMessage:
    """Preserve thread and allowlisted interaction reply routing metadata."""

    channel_id = getattr(route_envelope, "channel_id", None)
    thread_id = getattr(route_envelope, "thread_id", None)
    metadata = _merge_route_reply_metadata(reply.metadata, route_envelope)
    if channel_id and thread_id and reply.reply_to == thread_id:
        metadata.setdefault("channel", channel_id)
    if metadata == reply.metadata:
        return _sanitize_outgoing_message(reply)
    return _sanitize_outgoing_message(reply.model_copy(update={"metadata": metadata}))


def _status_reactor(channel: Any) -> Any:
    from opensquilla.channels._reactions import NULL_STATUS_REACTOR

    return getattr(channel, "status_reactor", NULL_STATUS_REACTOR)


def _streaming_reply_kwargs(channel: Any, msg: IncomingMessage) -> dict[str, Any]:
    builder = getattr(channel, "streaming_reply_kwargs", None)
    if not callable(builder):
        return {}
    return dict(builder(msg))


_STREAM_DONE = object()

# Coalescing window for consecutive text deltas in the relay queue. The
# relay yields a batched chunk once either threshold is reached. Both
# defaults are 0 so the relay preserves its historical one-chunk-per-delta
# behaviour out of the box; tuning either via ``config.task_runtime``
# enables coalescing for adapters that incur a per-call cost on
# ``send_streaming`` updates.
_STREAM_RELAY_DEFAULT_COALESCE_MS = 0.0
_STREAM_RELAY_DEFAULT_COALESCE_CHARS = 0


def _resolve_stream_relay_coalesce(config: Any) -> tuple[float, int]:
    """Return ``(window_seconds, char_threshold)`` for stream relay batching.

    ``None`` config or absent fields fall back to the module defaults so
    legacy call sites (tests, embedded use) keep their historical behaviour.
    """
    window_ms = _STREAM_RELAY_DEFAULT_COALESCE_MS
    char_threshold = _STREAM_RELAY_DEFAULT_COALESCE_CHARS
    runtime_cfg = getattr(config, "task_runtime", None) if config is not None else None
    cfg_window = getattr(runtime_cfg, "stream_relay_coalesce_ms", None)
    if isinstance(cfg_window, int | float) and cfg_window >= 0:
        window_ms = float(cfg_window)
    cfg_chars = getattr(runtime_cfg, "stream_relay_coalesce_chars", None)
    if isinstance(cfg_chars, int) and cfg_chars >= 0:
        char_threshold = cfg_chars
    return window_ms / 1000.0, char_threshold


class _RuntimeChannelStreamRelay:
    """Bridge one runtime task's stream events into a channel streaming adapter.

    The relay coalesces consecutive text deltas into larger chunks before
    handing them to ``send_streaming`` — adapters that incur a per-call cost
    (rate-limited message edits, network round trips) benefit from batching
    micro-deltas.  When ``send_streaming`` fails mid-stream the relay falls
    back to a single ``channel.send`` carrying the not-yet-delivered text so
    the user still sees the rest of the reply.
    """

    def __init__(self, channel: Any, inbound: IncomingMessage, config: Any = None) -> None:
        self._channel = channel
        self._inbound = inbound
        self._config = config
        self._queue: asyncio.Queue[str | object] = asyncio.Queue()
        self._artifacts: list[dict[str, Any]] = []
        self.delivered_artifact_keys: set[str] = set()
        self.attempted_artifact_keys: set[str] = set()
        self._attempted_artifact_ids: set[str] = set()
        self._task: asyncio.Task[Any] | None = None
        self._closed = False
        self._live_preview = _channel_can_replace_streamed_text(channel)
        self._text_deltas: list[str] = []
        self._done_snapshot_present = False
        self._done_snapshot_text = ""
        self._terminal_generation_reset = False
        self._stream_handle: _StreamedMessageHandle | None = None
        self.text_emitted = False
        self.stream_error: BaseException | None = None
        # Buffer of chunks already yielded to ``send_streaming``. If the
        # adapter raises mid-stream the relay falls back to ``channel.send``
        # with the chunks that never made it through.
        self._yielded_chunks: list[str] = []
        self._undelivered_index = 0
        coalesce_window_s, coalesce_chars = _resolve_stream_relay_coalesce(config)
        self._coalesce_window_s = coalesce_window_s
        self._coalesce_chars = coalesce_chars

    @classmethod
    def maybe_create(
        cls,
        channel: Any,
        inbound: IncomingMessage,
        task_runtime: Any,
        config: Any = None,
    ) -> _RuntimeChannelStreamRelay | None:
        if not resolve_channel_stream_policy(channel).relay_stream:
            return None
        enqueue = getattr(task_runtime, "enqueue", None)
        if not callable(enqueue) or not _accepts_keyword_arg(enqueue, "stream_event_sink"):
            return None
        return cls(channel, inbound, config)

    @classmethod
    def maybe_start(
        cls,
        channel: Any,
        inbound: IncomingMessage,
        task_runtime: Any,
        config: Any = None,
    ) -> _RuntimeChannelStreamRelay | None:
        relay = cls.maybe_create(channel, inbound, task_runtime, config)
        if relay is not None:
            relay.start()
        return relay

    def start(self) -> None:
        """Start external streaming only after durable turn acceptance."""

        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> Any:
        reply_kwargs = _streaming_reply_kwargs(self._channel, self._inbound)
        try:
            result = await self._channel.send_streaming(
                self._chunks(),
                **reply_kwargs,
            )
            return _streamed_message_handle(result, reply_kwargs)
        except Exception as exc:  # noqa: BLE001 - streaming is best-effort fallback.
            self.stream_error = exc
            log.warning(
                "channel_dispatch.runtime_streaming_failed",
                channel_type=type(self._channel).__name__,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None

    async def _coalesce_next_batch(
        self,
        first_text: str,
    ) -> tuple[str, object | None]:
        """Aggregate consecutive text items until window or char threshold.

        Returns ``(batched_text, sentinel_or_none)`` — when the trailing
        sentinel is ``_STREAM_DONE`` the caller flushes and exits.
        """
        if self._coalesce_window_s <= 0 and self._coalesce_chars <= 0:
            return first_text, None
        buffer = [first_text]
        size = len(first_text)
        deadline = (
            asyncio.get_event_loop().time() + self._coalesce_window_s
            if self._coalesce_window_s > 0
            else None
        )
        while True:
            if self._coalesce_chars and size >= self._coalesce_chars:
                return "".join(buffer), None
            remaining = deadline - asyncio.get_event_loop().time() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                return "".join(buffer), None
            try:
                if remaining is None:
                    item = self._queue.get_nowait()
                else:
                    item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except (asyncio.QueueEmpty, TimeoutError):
                return "".join(buffer), None
            if item is _STREAM_DONE:
                return "".join(buffer), _STREAM_DONE
            if isinstance(item, str):
                buffer.append(item)
                size += len(item)

    async def _chunks(self) -> AsyncIterator[str]:
        sanitizer = _DirectiveTagStreamSanitizer()
        while True:
            item = await self._queue.get()
            if item is _STREAM_DONE:
                tail = sanitizer.flush()
                if tail:
                    self._yielded_chunks.append(tail)
                    yield tail
                    # Only advance the delivered watermark when the consumer
                    # accepted the chunk (yield returned). If yield raises,
                    # the consumer failed to process it and the chunk must
                    # be replayed via the close() fallback path.
                    self._undelivered_index = len(self._yielded_chunks)
                return
            if not isinstance(item, str):
                continue
            batched, sentinel = await self._coalesce_next_batch(item)
            chunk = sanitizer.clean(batched)
            if chunk:
                self._yielded_chunks.append(chunk)
                yield chunk
                self._undelivered_index = len(self._yielded_chunks)
            if sentinel is _STREAM_DONE:
                tail = sanitizer.flush()
                if tail:
                    self._yielded_chunks.append(tail)
                    yield tail
                    self._undelivered_index = len(self._yielded_chunks)
                return

    async def emit(self, event: Any) -> None:
        artifact = _artifact_event_payload(event)
        if artifact is not None:
            self._artifacts.append(artifact)
            return
        generation_reset = _generation_reset_snapshot(event)
        if generation_reset is not None:
            terminal, snapshot_text = generation_reset
            # A reset invalidates every delta from the previous generation.
            # Keep only the authoritative replacement as the baseline for a
            # later generation or as the terminal snapshot for final failure.
            self._text_deltas[:] = [snapshot_text] if snapshot_text else []
            if terminal:
                self._done_snapshot_present = True
                self._done_snapshot_text = snapshot_text
                self._terminal_generation_reset = True
            return
        snapshot_present, snapshot_text = done_text_snapshot(event)
        if snapshot_present and (
            isinstance(event, DoneEvent)
            or getattr(event, "kind", None) == "done"
            or (isinstance(event, dict) and event.get("kind") == "done")
        ):
            self._done_snapshot_present = True
            self._done_snapshot_text = snapshot_text
            return
        text = _text_delta_from_event(event)
        if not text:
            return
        text = _strip_artifact_markers_from_channel_text(text)
        if not text:
            return
        self._text_deltas.append(text)
        if self._live_preview:
            self.text_emitted = True
            await self._queue.put(text)

    async def reconcile_final_text(self, text: str) -> bool:
        """Make the delivered channel message equal one canonical final value."""

        canonical = _sanitize_streamed_channel_text(text)
        streamed = "".join(self._yielded_chunks)
        if canonical == streamed:
            self.text_emitted = bool(canonical)
            return True
        replaced = await _replace_streamed_channel_text(
            self._channel,
            self._stream_handle,
            canonical,
        )
        if replaced:
            self._yielded_chunks[:] = [canonical] if canonical else []
            self._undelivered_index = len(self._yielded_chunks)
            self.text_emitted = bool(canonical)
        return replaced

    @property
    def has_terminal_snapshot(self) -> bool:
        return self._done_snapshot_present

    @property
    def has_terminal_generation_reset(self) -> bool:
        return self._terminal_generation_reset

    def attempted_artifact(self, artifact: dict[str, Any]) -> bool:
        artifact_id = artifact.get("id")
        if isinstance(artifact_id, str) and artifact_id in self._attempted_artifact_ids:
            return True
        key = _artifact_delivery_key(artifact)
        return bool(key and key in self.attempted_artifact_keys)

    async def close(self, timeout: float = 10.0) -> None:
        if self._closed:
            return
        self._closed = True
        artifact_lines = (
            []
            if _can_deliver_channel_files(self._channel)
            else _artifact_fallback_lines(self._artifacts)
        )
        terminal_text = (
            self._done_snapshot_text if self._done_snapshot_present else "".join(self._text_deltas)
        )
        if (
            not self._live_preview
            or (self._terminal_generation_reset and not self.text_emitted)
        ) and terminal_text:
            await self._queue.put(terminal_text)
            self.text_emitted = True
        if artifact_lines:
            prefix = "\n\n" if terminal_text else ""
            artifact_text = "\n".join(artifact_lines)
            await self._queue.put(f"{prefix}{artifact_text}")
            self.text_emitted = True
        await self._queue.put(_STREAM_DONE)
        if self._task is None:
            return
        try:
            self._stream_handle = await asyncio.wait_for(
                asyncio.shield(self._task), timeout=timeout
            )
        except TimeoutError as exc:
            self.stream_error = exc
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        except Exception as exc:  # noqa: BLE001 - error already becomes batch fallback.
            self.stream_error = exc

        if self.stream_error is None and self._done_snapshot_present:
            canonical_with_artifacts = _sanitize_streamed_channel_text(terminal_text)
            if artifact_lines:
                artifact_text = "\n".join(artifact_lines)
                canonical_with_artifacts = "\n\n".join(
                    part for part in (canonical_with_artifacts, artifact_text) if part
                )
            if not await self.reconcile_final_text(canonical_with_artifacts):
                self.stream_error = RuntimeError(
                    "streamed channel reply could not apply terminal text snapshot"
                )

        # Per-event delivery fallback: when send_streaming raised mid-stream,
        # any chunk that was queued but never reached the consumer must
        # still land via channel.send. Drain the relay queue for queued
        # text items, concatenate with chunks already yielded but not
        # delivered, and send as a single batch reply. Successful streams
        # (stream_error is None) skip this branch.
        if self.stream_error is not None:
            queued_remainder: list[str] = []
            while True:
                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is _STREAM_DONE:
                    continue
                if isinstance(item, str):
                    queued_remainder.append(item)
            undelivered_yielded = "".join(self._yielded_chunks[self._undelivered_index :])
            fallback_text = undelivered_yielded + "".join(queued_remainder)
            if fallback_text:
                try:
                    await self._channel.send(
                        _build_reply_message(
                            self._channel,
                            fallback_text,
                            self._inbound,
                        )
                    )
                except Exception as send_exc:  # noqa: BLE001 - log only.
                    log.warning(
                        "channel_dispatch.stream_relay_batch_fallback_failed",
                        channel_type=type(self._channel).__name__,
                        error_type=type(send_exc).__name__,
                        error=str(send_exc),
                    )
                self._undelivered_index = len(self._yielded_chunks)

        if _can_deliver_channel_files(self._channel):
            self.attempted_artifact_keys.update(
                key
                for artifact in self._artifacts
                if (key := _artifact_delivery_key(artifact))
            )
            self._attempted_artifact_ids.update(
                artifact_id
                for artifact in self._artifacts
                if isinstance((artifact_id := artifact.get("id")), str)
                and artifact_id
            )
            undelivered = await _deliver_artifacts_as_channel_files(
                self._channel,
                self._inbound,
                self._artifacts,
                self._config,
            )
            undelivered_keys = {
                key for artifact in undelivered if (key := _artifact_delivery_key(artifact))
            }
            self.delivered_artifact_keys.update(
                key
                for artifact in self._artifacts
                if (key := _artifact_delivery_key(artifact)) and key not in undelivered_keys
            )
            fallback_lines = _artifact_fallback_lines(undelivered)
            if fallback_lines:
                await self._channel.send(
                    _build_reply_message(
                        self._channel,
                        "\n".join(fallback_lines),
                        self._inbound,
                    )
                )


def _text_delta_from_event(event: Any) -> str:
    if isinstance(event, TextDeltaEvent):
        return event.text
    kind = getattr(event, "kind", None)
    if kind == "text_delta":
        text = getattr(event, "text", "")
        return text if isinstance(text, str) else ""
    if isinstance(event, dict) and event.get("kind") == "text_delta":
        text = event.get("text", "")
        return text if isinstance(text, str) else ""
    return ""


def _generation_reset_snapshot(event: Any) -> tuple[bool, str] | None:
    """Return the public reset terminal flag and authoritative replacement."""

    terminal_text: object = None
    authoritative_text: object = ""
    if isinstance(event, AnswerGenerationResetEvent):
        terminal = bool(event.terminal)
        terminal_text = event.terminal_text_snapshot
        authoritative_text = event.authoritative_text_snapshot
    elif isinstance(event, dict) and event.get("kind") == "answer_generation_reset":
        terminal = bool(event.get("terminal", False))
        terminal_text = event.get("terminal_text_snapshot")
        authoritative_text = event.get("authoritative_text_snapshot")
    elif getattr(event, "kind", None) == "answer_generation_reset":
        terminal = bool(getattr(event, "terminal", False))
        terminal_text = getattr(event, "terminal_text_snapshot", None)
        authoritative_text = getattr(event, "authoritative_text_snapshot", "")
    else:
        return None

    if terminal and isinstance(terminal_text, str) and terminal_text:
        return True, terminal_text
    if isinstance(authoritative_text, str) and authoritative_text:
        return terminal, authoritative_text
    if terminal:
        return True, "The model could not complete this answer."
    return False, ""


def _artifact_event_payload(event: Any) -> dict[str, Any] | None:
    if isinstance(event, ArtifactEvent):
        return artifact_payload(event)
    if isinstance(event, dict) and event.get("kind") == "artifact":
        return artifact_payload(event)
    if getattr(event, "kind", None) == "artifact":
        return artifact_payload(event)
    return None


def _router_decision_payload(event: RouterDecisionEvent) -> dict[str, Any]:
    return {
        "tier": event.tier,
        "tier_index": event.tier_index,
        "model": event.model,
        "baseline_model": event.baseline_model,
        "source": event.source,
        "confidence": event.confidence,
        "probs": list(event.probs),
        "savings_pct": event.savings_pct,
        "fallback": event.fallback,
        "thinking_mode": event.thinking_mode,
        "prompt_policy": event.prompt_policy,
        "routing_applied": event.routing_applied,
        "rollout_phase": event.rollout_phase,
        "context_window": event.context_window,
    }


def _terminal_generation_reset_text(event: AnswerGenerationResetEvent) -> str:
    """Return the one safe user-visible terminal replacement for a reset."""

    terminal_text = event.terminal_text_snapshot
    if isinstance(terminal_text, str) and terminal_text:
        return terminal_text
    if event.authoritative_text_snapshot:
        return event.authoritative_text_snapshot
    return "The model could not complete this answer."


async def _emit_generation_reset(
    event_bridge: EventBridge | None,
    session_key: str,
    event: AnswerGenerationResetEvent,
) -> None:
    if event_bridge is None:
        return
    # Serialize through the public wire boundary so the internal terminal
    # failure metadata used for persistence never reaches subscribers.
    from opensquilla.gateway.protocol import serialize_public_event

    payload = serialize_public_event(event)
    payload.pop("kind", None)
    await event_bridge.emit(
        session_key,
        "session.event.answer_generation_reset",
        payload,
    )


def _ensemble_progress_payload(event: EnsembleProgressEvent) -> dict[str, Any]:
    return {
        "event_type": event.event_type,
        "proposer_index": event.proposer_index,
        "proposer_label": event.proposer_label,
        "proposer_model": event.proposer_model,
        "proposer_provider": event.proposer_provider,
        "sample_index": event.sample_index,
        "elapsed_ms": event.elapsed_ms,
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "cost_usd": event.cost_usd,
        "error": event.error,
    }


def _tool_use_start_payload(event: ToolUseStartEvent) -> dict[str, Any]:
    return {
        "tool_use_id": event.tool_use_id,
        "tool_name": event.tool_name,
        "name": event.tool_name,
        "synthetic_from_text": event.synthetic_from_text,
    }


def _tool_use_delta_payload(event: ToolUseDeltaEvent) -> dict[str, Any]:
    return {
        "tool_use_id": event.tool_use_id,
        "json_fragment": event.json_fragment,
    }


def _tool_use_end_payload(event: ToolUseEndEvent) -> dict[str, Any]:
    return {
        "tool_use_id": event.tool_use_id,
        "tool_name": event.tool_name,
        "name": event.tool_name,
        "arguments": event.arguments,
        "input": event.arguments,
        "synthetic_from_text": event.synthetic_from_text,
    }


def _tool_result_payload(event: ToolResultEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool_use_id": event.tool_use_id,
        "tool_name": event.tool_name,
        "name": event.tool_name,
        "result": event.result,
        "is_error": event.is_error,
    }
    if event.arguments is not None:
        payload["arguments"] = event.arguments
    if event.execution_status is not None:
        payload["execution_status"] = normalize_execution_status(event.execution_status)
    return payload


def _clarify_tool_arguments(event: ToolResultEvent) -> dict[str, Any] | None:
    candidates: list[Any] = [event.arguments]
    if isinstance(event.result, str):
        try:
            candidates.append(json.loads(event.result))
        except (json.JSONDecodeError, TypeError):
            pass
    else:
        candidates.append(event.result)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        schema = candidate.get("clarify_schema")
        if (
            candidate.get("kind") == "user_input"
            and candidate.get("paused") is True
            and isinstance(schema, dict)
        ):
            return candidate
    return None


def _channel_accepts_metadata_card(channel: Any) -> bool:
    profile = channel_capability_profile(channel)
    if profile is None:
        return bool(getattr(channel, "supports_clarify_cards", False))
    if not (profile.cards or profile.interactive_cards or profile.card_actions):
        return False
    return profile.channel_type == "feishu" or bool(
        getattr(channel, "supports_clarify_cards", False)
    )


def _clarify_field_label(field: dict[str, Any]) -> str:
    name = str(field.get("name") or "").strip()
    prompt = str(field.get("prompt") or "").strip()
    if prompt and prompt != name:
        return f"{name} - {prompt}"
    return name or prompt or "field"


def _clarify_field_required_text(field: dict[str, Any]) -> str:
    if field.get("required") is True:
        return "required"
    if field.get("default") not in (None, ""):
        return f"default: {field['default']}"
    return "optional"


def _clarify_field_element(field: dict[str, Any]) -> dict[str, Any] | None:
    name = str(field.get("name") or "").strip()
    if not name:
        return None
    label = _clarify_field_label(field)
    placeholder = {
        "tag": "plain_text",
        "content": label,
    }
    field_type = str(field.get("type") or "string").lower()
    if field_type == "enum" and isinstance(field.get("choices"), list):
        options: list[dict[str, Any]] = []
        for choice in field["choices"]:
            rendered = str(choice)
            options.append(
                {
                    "text": {"tag": "plain_text", "content": rendered},
                    "value": rendered,
                }
            )
        return {
            "tag": "select_static",
            "name": name,
            "placeholder": placeholder,
            "options": options,
        }
    if field_type == "bool":
        return {
            "tag": "select_static",
            "name": name,
            "placeholder": placeholder,
            "options": [
                {"text": {"tag": "plain_text", "content": "true"}, "value": "true"},
                {"text": {"tag": "plain_text", "content": "false"}, "value": "false"},
            ],
        }
    return {
        "tag": "input",
        "name": name,
        "placeholder": placeholder,
    }


def _build_clarify_channel_card(args: dict[str, Any], msg: IncomingMessage) -> dict[str, Any]:
    schema = cast(dict[str, Any], args["clarify_schema"])
    fields = schema.get("fields")
    if not isinstance(fields, list):
        fields = []
    intro = str(schema.get("intro") or "").strip()
    elements: list[dict[str, Any]] = []
    if intro:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": intro}})

    rows: list[str] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        rows.append(f"- **{_clarify_field_label(field)}** ({_clarify_field_required_text(field)})")
    if rows:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**Fields**\n" + "\n".join(rows),
                },
            }
        )
    for field in fields:
        if isinstance(field, dict) and (element := _clarify_field_element(field)):
            elements.append(element)

    value: dict[str, Any] = {
        "opensquilla_action": "clarify_submit",
        "channel_id": msg.channel_id,
    }
    is_group = msg.metadata.get("is_group")
    if isinstance(is_group, bool):
        value["is_group"] = is_group
    chat_type = msg.metadata.get("chat_type")
    if isinstance(chat_type, str) and chat_type:
        value["chat_type"] = chat_type
    if isinstance(args.get("run_id"), str) and args["run_id"]:
        value["run_id"] = args["run_id"]
    if isinstance(args.get("step"), str) and args["step"]:
        value["step"] = args["step"]

    cancel_keywords = schema.get("cancel_keywords")
    if isinstance(cancel_keywords, list) and cancel_keywords:
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "Cancel: " + " / ".join(str(item) for item in cancel_keywords),
                    }
                ],
            }
        )

    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "Submit"},
                    "type": "primary",
                    "value": value,
                }
            ],
        }
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "需要补充信息"},
        },
        "elements": elements,
    }


async def _maybe_send_clarify_channel_card(
    channel: Any,
    msg: IncomingMessage,
    event: ToolResultEvent,
) -> bool:
    args = _clarify_tool_arguments(event)
    if args is None or not _channel_accepts_metadata_card(channel):
        return False
    card = _build_clarify_channel_card(args, msg)
    try:
        await channel.send(
            OutgoingMessage(
                content="OpenSquilla clarification form",
                reply_to=msg.channel_id,
                metadata={"card": card},
            )
        )
    except Exception as exc:  # noqa: BLE001 - keep text fallback available
        log.warning(
            "channel_dispatch.clarify_card_send_failed",
            channel_type=type(channel).__name__,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return False
    return True


async def _read_transcript_rows(session_manager: Any, session_key: str) -> list[Any]:
    read_transcript = getattr(session_manager, "read_transcript", None)
    if not callable(read_transcript):
        return []
    try:
        rows = await read_transcript(session_key)
    except Exception:
        log.warning("channel_dispatch.read_transcript_failed", session_key=session_key)
        return []
    return list(rows or [])


async def _transcript_watermark(session_manager: Any, session_key: str) -> int:
    return len(await _read_transcript_rows(session_manager, session_key))


def _dump_attachment(attachment: Any) -> dict[str, Any] | None:
    if isinstance(attachment, dict):
        return dict(attachment)
    model_dump = getattr(attachment, "model_dump", None)
    if callable(model_dump):
        # Keep Pydantic's Python-mode default so bytes remain bytes for shared ingest.
        dumped = model_dump()
        return dict(dumped) if isinstance(dumped, dict) else None
    return None


async def _materialize_channel_attachments(channel: Any, attachments: list[Any]) -> list[Any]:
    resolver = getattr(channel, "resolve_inbound_attachment", None)
    if not callable(resolver):
        return list(attachments or [])

    materialized: list[Any] = []
    for attachment in attachments or []:
        try:
            resolved = resolver(attachment)
            if inspect.isawaitable(resolved):
                resolved = await resolved
            materialized.append(resolved if resolved is not None else attachment)
        except Exception as exc:  # noqa: BLE001 - failure degrades via shared ingest marker
            item = _dump_attachment(attachment) or {"name": "attachment"}
            item["_ingest_error"] = str(exc)
            materialized.append(item)
    return materialized


async def _ingest_channel_message_attachments(
    *,
    channel: Any,
    msg: IncomingMessage,
    config: Any = None,
) -> AttachmentIngestResult:
    materialized = await _materialize_channel_attachments(
        channel,
        list(getattr(msg, "attachments", []) or []),
    )
    attachments_cfg = getattr(config, "attachments", None)
    opaque_cap = getattr(attachments_cfg, "opaque_max_bytes", None)
    result = await ingest_attachments(
        msg.content,
        materialized,
        failure_mode="mark",
        mark_bytes_as_staged=True,
        accept_opaque=bool(getattr(attachments_cfg, "accept_opaque", True)),
        opaque_limit_bytes=opaque_cap if isinstance(opaque_cap, int) else None,
    )
    for failure in result.failures:
        log.warning(
            "channel.attachment_ingest_failed",
            channel=getattr(channel, "channel_id", None) or type(channel).__name__,
            attachment_index=failure.index,
            attachment_name=failure.name,
            reason=failure.reason,
            detail=failure.detail,
        )
    return result


def _supports_atomic_channel_acceptance(session_manager: Any, task_runtime: Any) -> bool:
    """Use the durable path only for the concrete production services."""

    from opensquilla.gateway.task_runtime import TaskRuntime
    from opensquilla.session.storage import SessionStorage

    return isinstance(getattr(session_manager, "storage", None), SessionStorage) and isinstance(
        task_runtime, TaskRuntime
    )


def _channel_native_request_id(msg: IncomingMessage) -> str | None:
    metadata = dict(getattr(msg, "metadata", None) or {})
    if metadata.get("_opensquilla_debounce_native_ids_incomplete") is True:
        return None
    aggregate_ids = metadata.get("_opensquilla_debounce_native_message_ids")
    if isinstance(aggregate_ids, list) and aggregate_ids:
        normalized = [str(value).strip() for value in aggregate_ids if str(value).strip()]
        if normalized:
            digest = hashlib.sha256(
                json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()
            return f"debounce:{digest}"

    aliases = (
        "native_message_id",
        "message_id",
        "msg_id",
        "event_id",
        "activity_id",
        "update_id",
        "ts",
    )
    for name in aliases:
        value = metadata.get(name)
        if value is not None and str(value).strip():
            return f"{name}:{str(value).strip()}"
    return None


def _channel_ingress_identity(
    *,
    msg: IncomingMessage,
    route_envelope: Any,
    session_key: str,
    raw_content: str,
) -> Any:
    from opensquilla.gateway.turn_ingress import request_identity

    source_name = str(getattr(route_envelope, "source_name", None) or "unknown")
    account_id = str(getattr(route_envelope, "account_id", None) or "default")
    raw_scope = f"channel:{source_name}:{account_id}"
    if len(raw_scope) > 256:
        raw_scope = f"channel:sha256:{hashlib.sha256(raw_scope.encode()).hexdigest()}"

    params: dict[str, Any] = {
        "message": raw_content,
        "attachments": [
            dumped
            for attachment in list(getattr(msg, "attachments", None) or [])
            if (dumped := _dump_attachment(attachment)) is not None
        ],
        # Deliberately constant: the resolved busy-input mode depends on
        # channel config and runtime capabilities, so folding it into the
        # idempotency fingerprint would break replay matching across restarts
        # and against receipts recorded before the mode was configurable.
        "queueMode": "followup",
        "runKind": "channel_turn",
        "inputProvenance": dict(getattr(route_envelope, "input_provenance", None) or {}),
    }
    native_request_id = _channel_native_request_id(msg)
    if native_request_id is not None:
        params["clientRequestId"] = native_request_id
    else:
        metadata = getattr(msg, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            msg.metadata = metadata
        fallback = metadata.get("_opensquilla_client_request_id")
        if isinstance(fallback, str) and fallback:
            params["clientRequestId"] = fallback

    identity = request_identity(
        params,
        request_session_key=session_key,
        source_scope=raw_scope,
    )
    if native_request_id is None:
        msg.metadata["_opensquilla_client_request_id"] = identity.client_request_id
        log.warning(
            "channel.ingress_missing_native_message_id",
            session_key=session_key,
            channel_type=getattr(route_envelope, "channel_type", None),
        )
    return identity


async def _prepare_channel_user_message(
    *,
    session_manager: Any,
    session_key: str,
    session_node: Any,
    text: str,
    attachments: list[dict[str, Any]],
    config: Any,
) -> tuple[Any, int, str]:
    persist_content = text
    persisted_text = text
    if attachments:
        from opensquilla.gateway.transcripts import build_transcript_attachment_envelope

        if hasattr(session_manager, "stamp_user_text"):
            stamped = session_manager.stamp_user_text(text)
            if isinstance(stamped, str):
                persisted_text = stamped

        attachments_cfg = getattr(config, "attachments", None)
        persist_enabled = bool(getattr(attachments_cfg, "persist_transcripts", True))
        media_root = media_root_from_config(config)
        disk_budget = getattr(attachments_cfg, "transcript_disk_budget_bytes", None)
        session_id = session_key.split(":")[-1] or session_key
        persist_content, _writes = build_transcript_attachment_envelope(
            text=persisted_text,
            attachments=attachments,
            session_id=session_id,
            media_root=media_root,
            persist_enabled=persist_enabled,
            disk_budget_bytes=disk_budget if isinstance(disk_budget, int) else None,
        )

    entry, expected_epoch = await session_manager.prepare_message(
        session_key,
        role="user",
        content=persist_content,
        session_node=session_node,
    )
    if not attachments and isinstance(getattr(entry, "content", None), str):
        persisted_text = entry.content
    return entry, expected_epoch, persisted_text


async def _record_main_delivery_context_after_acceptance(
    session_manager: Any,
    *,
    session_key: str,
    route_envelope: Any,
) -> None:
    """Maintain the main-session delivery fallback after acceptance commits."""

    from opensquilla.gateway.routing import delivery_fields_from_envelope
    from opensquilla.session.keys import build_main_key, parse_agent_id

    agent_id = parse_agent_id(session_key)
    main_session_key = build_main_key(agent_id)
    if main_session_key == session_key:
        return
    fields = delivery_fields_from_envelope(route_envelope)
    try:
        _main, created = await session_manager.get_or_create(
            main_session_key,
            agent_id=agent_id,
            **fields,
        )
        if not created:
            await session_manager.update(main_session_key, **fields)
    except Exception:  # noqa: BLE001 - turn acceptance is already durable.
        log.warning(
            "channel.main_delivery_context_update_failed",
            session_key=session_key,
            exc_info=True,
        )


async def _accept_channel_runtime_turn_impl(
    *,
    channel: Any,
    msg: IncomingMessage,
    session_manager: Any,
    session_key: str,
    route_envelope: Any,
    task_runtime: Any,
    ingested: AttachmentIngestResult,
    raw_content: str,
    config: Any,
    busy_input_mode: str = "followup",
) -> tuple[Any | None, str, _RuntimeChannelStreamRelay | None, bool]:
    """Atomically accept a channel message, task, and idempotency receipt."""

    from opensquilla.gateway.routing import delivery_fields_from_envelope
    from opensquilla.gateway.task_runtime import TaskHandle
    from opensquilla.session.manager import SessionIntent
    from opensquilla.session.models import AgentTaskStatus

    def _accepted_replay_handle(acceptance: Any) -> TaskHandle | None:
        """Attach redelivery to any accepted task instead of silently acking it.

        Channel delivery is intentionally at-least-once: if the first inbound
        coroutine disappears after durable acceptance but before registering
        its reply waiter, a duplicate native message must be able to wait for
        the existing queued/running task. Two live waiters can occasionally
        duplicate an external send, which is safer than losing the reply.
        """

        task_id = acceptance.receipt.task_id
        status = acceptance.task_status
        if task_id is None or status not in {
            AgentTaskStatus.QUEUED,
            AgentTaskStatus.RUNNING,
            AgentTaskStatus.SUCCEEDED,
            AgentTaskStatus.FAILED,
            AgentTaskStatus.CANCELLED,
            AgentTaskStatus.TIMEOUT,
            AgentTaskStatus.ABANDONED,
        }:
            return None
        return TaskHandle(
            task_id=task_id,
            session_key=acceptance.receipt.accepted_session_key,
            status=status,
        )

    storage = session_manager.storage
    identity = _channel_ingress_identity(
        msg=msg,
        route_envelope=route_envelope,
        session_key=session_key,
        raw_content=raw_content,
    )
    existing = await storage.get_turn_ingress_receipt(
        source_scope=identity.source_scope,
        request_session_key=identity.request_session_key,
        client_request_id=identity.client_request_id,
    )
    if existing is not None:
        if existing.receipt.request_fingerprint != identity.request_fingerprint:
            from opensquilla.session.storage import TurnIngressConflictError

            raise TurnIngressConflictError(
                "channel message id was already accepted with different content"
            )
        return _accepted_replay_handle(existing), msg.content, None, True

    delivery_fields = delivery_fields_from_envelope(route_envelope)
    intent_plan = await session_manager.prepare_intent(
        session_key,
        SessionIntent.CONTINUE,
        agent_id=route_envelope.agent_id,
        **delivery_fields,
    )
    from opensquilla.session.goals import ClaimGoalMutation, GoalClaimCandidate

    goal_claim_candidate: GoalClaimCandidate | None = None
    current_goal = await storage.get_goal(session_key)
    if current_goal is not None and current_goal.status == "active":
        goal_claim_candidate = GoalClaimCandidate(
            session_id=current_goal.session_id,
            epoch=current_goal.session_epoch,
            goal_id=current_goal.goal_id,
        )
    workspace_guard = None
    bound_workspace_id = getattr(intent_plan.node, "workspace_id", None)
    if isinstance(bound_workspace_id, str) and bound_workspace_id:
        from opensquilla.project_workspaces import (
            resolve_validated_project_workspace,
        )

        validated_workspace = await resolve_validated_project_workspace(
            storage,
            bound_workspace_id,
        )
        workspace_guard = validated_workspace.guard
    entry, expected_epoch, persisted_text = await _prepare_channel_user_message(
        session_manager=session_manager,
        session_key=session_key,
        session_node=intent_plan.node,
        text=ingested.text,
        attachments=ingested.attachments,
        config=config,
    )
    stream_relay = _RuntimeChannelStreamRelay.maybe_create(
        channel,
        msg,
        task_runtime,
        config,
    )
    overflow_policy = _resolve_channel_overflow_policy(channel, config)

    async def _commit_and_activate() -> tuple[
        Any | None,
        str,
        _RuntimeChannelStreamRelay | None,
        bool,
    ]:
        nonlocal stream_relay
        reservation = await reserve_turn_via_runtime(
            task_runtime,
            route_envelope,
            msg.content,
            attachments=ingested.attachments,
            mode=_resolve_channel_busy_input_mode(task_runtime, busy_input_mode),
            run_kind="channel_turn",
            semantic_message=raw_content,
            stream_event_sink=(
                stream_relay.emit if stream_relay is not None else None
            ),
            overflow_policy=overflow_policy,
            goal_candidate=(
                goal_claim_candidate.as_task_detail()
                if goal_claim_candidate is not None
                else None
            ),
        )
        try:
            acceptance = await storage.accept_turn(
                entry,
                expected_epoch=expected_epoch,
                updated_at=int(time.time() * 1000),
                task_record=reservation.task_record,
                source_scope=identity.source_scope,
                request_session_key=identity.request_session_key,
                client_request_id=identity.client_request_id,
                request_fingerprint=identity.request_fingerprint,
                session_node=intent_plan.node if intent_plan.action == "create" else None,
                session_updates=delivery_fields,
                workspace_guard=workspace_guard,
                goal_mutation=(
                    ClaimGoalMutation(candidate=goal_claim_candidate)
                    if goal_claim_candidate is not None
                    else None
                ),
            )
        except BaseException:
            await task_runtime.abort_reservation(reservation)
            raise

        if acceptance.replayed:
            await task_runtime.abort_reservation(reservation)
            return _accepted_replay_handle(acceptance), persisted_text, None, True

        if stream_relay is not None:
            try:
                stream_relay.start()
            except Exception:  # noqa: BLE001 - turn is already accepted.
                log.warning(
                    "channel.stream_relay_start_failed",
                    session_key=session_key,
                    task_id=acceptance.receipt.task_id,
                    exc_info=True,
                )
                stream_relay = None
        try:
            handle = await task_runtime.activate(
                reservation,
                persisted_user_message_id=acceptance.receipt.message_id,
                fresh_user_session=acceptance.fresh_user_session,
            )
        except Exception as exc:  # noqa: BLE001 - acceptance already committed.
            log.error(
                "channel.turn_activation_failed",
                session_key=session_key,
                task_id=acceptance.receipt.task_id,
                exc_info=True,
            )
            if reservation.activated:
                log.warning(
                    "channel.turn_activation_error_after_start",
                    session_key=session_key,
                    task_id=acceptance.receipt.task_id,
                )
                handle = await task_runtime.activate(reservation)
            else:
                try:
                    await task_runtime.abort_reservation(reservation)
                except Exception:  # noqa: BLE001 - preserve accepted channel handling.
                    log.warning(
                        "channel.turn_activation_abort_failed",
                        session_key=session_key,
                        task_id=acceptance.receipt.task_id,
                        exc_info=True,
                    )
                goal_compensated = False
                goal_service = getattr(task_runtime, "goal_service", None)
                compensate_goal = getattr(
                    goal_service,
                    "compensate_activation_failure",
                    None,
                )
                if acceptance.goal_context is not None and callable(compensate_goal):
                    try:
                        await compensate_goal(acceptance.goal_context.as_task_detail())
                        goal_compensated = True
                    except Exception:  # noqa: BLE001 - preserve accepted handling.
                        log.warning(
                            "channel.goal_activation_compensation_failed",
                            session_key=session_key,
                            task_id=acceptance.receipt.task_id,
                            exc_info=True,
                        )
                if not goal_compensated:
                    try:
                        await storage.update_agent_task(
                            acceptance.receipt.task_id,
                            status="failed",
                            finished_at=int(time.time() * 1000),
                            terminal_reason="activation_failed",
                            error_class=type(exc).__name__,
                            error_message=str(exc),
                        )
                    except Exception:  # noqa: BLE001 - preserve accepted handling.
                        log.warning(
                            "channel.turn_activation_failure_record_failed",
                            session_key=session_key,
                            task_id=acceptance.receipt.task_id,
                            exc_info=True,
                        )
                handle = TaskHandle(
                    task_id=acceptance.receipt.task_id,
                    session_key=acceptance.receipt.accepted_session_key,
                    status=AgentTaskStatus.FAILED,
                )

        try:
            session_manager.notify_message_appended(entry)
        except Exception:  # noqa: BLE001 - turn is already accepted.
            log.warning(
                "channel.post_accept_notify_failed",
                session_key=session_key,
                task_id=acceptance.receipt.task_id,
                exc_info=True,
            )
        await _record_main_delivery_context_after_acceptance(
            session_manager,
            session_key=session_key,
            route_envelope=route_envelope,
        )
        return handle, persisted_text, stream_relay, False

    async def _commit_with_session_admission() -> tuple[
        Any | None,
        str,
        _RuntimeChannelStreamRelay | None,
        bool,
    ]:
        async with task_runtime.collect_admission(route_envelope.session_key):
            return await _commit_and_activate()

    return await complete_durable_ingress(_commit_with_session_admission())


async def _accept_channel_runtime_turn(
    *,
    channel: Any,
    msg: IncomingMessage,
    session_manager: Any,
    session_key: str,
    route_envelope: Any,
    task_runtime: Any,
    ingested: AttachmentIngestResult | None,
    raw_content: str,
    config: Any,
    principal_is_owner: bool | None = None,
    busy_input_mode: str = "followup",
) -> tuple[Any | None, str, _RuntimeChannelStreamRelay | None, bool]:
    """Fence user intent before channel session/workspace preparation."""

    async with task_runtime.explicit_ingress_intent(route_envelope.session_key):
        if ingested is None:
            assert principal_is_owner is not None
            await _apply_saved_channel_run_context(
                route_envelope,
                session_manager=session_manager,
                config=config,
                workspace_dir=None,
                principal_is_owner=principal_is_owner,
            )
            ingested = await _ingest_channel_message_attachments(
                channel=channel,
                msg=msg,
                config=config,
            )
        return await _accept_channel_runtime_turn_impl(
            channel=channel,
            msg=msg,
            session_manager=session_manager,
            session_key=session_key,
            route_envelope=route_envelope,
            task_runtime=task_runtime,
            ingested=ingested,
            raw_content=raw_content,
            config=config,
            busy_input_mode=busy_input_mode,
        )


async def _append_channel_user_message(
    *,
    session_manager: Any,
    session_key: str,
    text: str,
    attachments: list[dict[str, Any]],
    config: Any,
) -> tuple[Any, str]:
    if attachments:
        from opensquilla.gateway.transcripts import build_transcript_attachment_envelope

        stamped_text = text
        if hasattr(session_manager, "stamp_user_text"):
            stamped = session_manager.stamp_user_text(text)
            if isinstance(stamped, str):
                stamped_text = stamped

        attachments_cfg = getattr(config, "attachments", None)
        persist_enabled = bool(getattr(attachments_cfg, "persist_transcripts", True))
        media_root = media_root_from_config(config)
        disk_budget = getattr(attachments_cfg, "transcript_disk_budget_bytes", None)
        session_id = session_key.split(":")[-1] or session_key
        envelope, _writes = build_transcript_attachment_envelope(
            text=stamped_text,
            attachments=attachments,
            session_id=session_id,
            media_root=media_root,
            persist_enabled=persist_enabled,
            disk_budget_bytes=disk_budget if isinstance(disk_budget, int) else None,
        )
        persisted = await session_manager.append_message(session_key, role="user", content=envelope)
        return persisted, stamped_text

    persisted = await session_manager.append_message(session_key, role="user", content=text)
    if persisted is not None and isinstance(persisted.content, str):
        return persisted, persisted.content
    return persisted, text


async def _latest_assistant_text_after(
    session_manager: Any,
    session_key: str,
    start_index: int,
) -> str:
    rows = await _read_transcript_rows(session_manager, session_key)
    for row in reversed(rows[start_index:]):
        role = row.get("role") if isinstance(row, dict) else getattr(row, "role", None)
        content = row.get("content") if isinstance(row, dict) else getattr(row, "content", None)
        if role == "assistant" and isinstance(content, str) and content:
            return content
    return ""


async def _replayed_assistant_text(
    session_manager: Any,
    session_key: str,
    task_record: Any,
) -> str | None:
    """Resolve the exact assistant row recorded when a channel task completed."""

    details = getattr(task_record, "details", None)
    if isinstance(details, dict):
        durable_content = details.get("terminal_assistant_message_content")
        if isinstance(durable_content, str):
            return durable_content
    message_id = details.get("terminal_assistant_message_id") if isinstance(details, dict) else None
    if not isinstance(message_id, str) or not message_id:
        return None
    get_canonical_transcript = getattr(session_manager, "get_canonical_transcript", None)
    if not callable(get_canonical_transcript):
        return None
    try:
        rows = await get_canonical_transcript(session_key)
    except Exception:  # noqa: BLE001 - replay falls back to an actionable notice.
        log.warning(
            "channel_dispatch.replay_transcript_read_failed",
            session_key=session_key,
            task_id=getattr(task_record, "task_id", None),
            exc_info=True,
        )
        return None
    for row in rows or []:
        row_message_id = (
            row.get("message_id") if isinstance(row, dict) else getattr(row, "message_id", None)
        )
        if row_message_id != message_id:
            continue
        role = row.get("role") if isinstance(row, dict) else getattr(row, "role", None)
        content = row.get("content") if isinstance(row, dict) else getattr(row, "content", None)
        if role == "assistant" and isinstance(content, str):
            return content
        return None
    return None


def _status_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _build_runtime_reply_message(
    channel: Any,
    content: str,
    inbound: IncomingMessage,
    route_envelope: Any,
) -> OutgoingMessage:
    builder = getattr(channel, "build_reply_message", None)
    if callable(builder):
        reply = builder(content, inbound)
        if isinstance(reply, OutgoingMessage):
            return _sanitize_outgoing_message(reply)

    target = getattr(route_envelope, "reply_target", None)
    if target is not None and getattr(target, "kind", None) == "channel":
        channel_name = getattr(target, "channel_name", None)
        channel_id = getattr(target, "to", None)
        thread_id = getattr(target, "thread_id", None)
        if channel_name == "slack":
            metadata = {"channel": channel_id} if channel_id else {}
            if thread_id:
                return _sanitize_outgoing_message(
                    OutgoingMessage(content=content, reply_to=thread_id, metadata=metadata)
                )
            if channel_id:
                return _sanitize_outgoing_message(
                    OutgoingMessage(
                        content=content,
                        reply_to=None,
                        metadata={**metadata, "thread_ts": None},
                    )
                )
        return _sanitize_outgoing_message(
            OutgoingMessage(content=content, reply_to=thread_id or channel_id)
        )

    return _build_reply_message(channel, content, inbound)


#: Attempts for a user-visible reply whose failure class can still succeed.
#: Small on purpose: the answer is already computed, the user is waiting, and
#: an unrecoverable channel should surface fast rather than stall the turn.
_REPLY_SEND_ATTEMPTS: int = 3
_REPLY_RETRY_BASE_DELAY_S: float = 1.0
#: Same defensive ceiling the HTTP retry loop applies: a provider's pacing
#: hint is honored, but cannot hold a finished answer hostage for hours.
_REPLY_RETRY_MAX_DELAY_S: float = 60.0

#: Failure classes where *any* send to this target fails, so a delivery-failure
#: notice would fail identically — attempting one just burns another call.
_REPLY_NOTICE_HOPELESS_CLASSES: frozenset[str] = frozenset({"auth_invalid", "target_missing"})


def _reply_retry_delay(error: BaseException, attempt: int) -> float:
    """Backoff before re-sending a reply, honoring a provider pacing hint."""
    hint = getattr(error, "retry_after", None)
    if isinstance(hint, int | float) and not isinstance(hint, bool) and hint >= 0:
        return min(float(hint), _REPLY_RETRY_MAX_DELAY_S)
    backoff: float = _REPLY_RETRY_BASE_DELAY_S * (2**attempt)
    return min(backoff, _REPLY_RETRY_MAX_DELAY_S)


async def _send_channel_reply_guarded(
    channel: Any,
    message: OutgoingMessage,
    *,
    session_key: str,
) -> str | None:
    """Deliver a user-visible reply, surviving a provider send failure.

    An unguarded send loses a fully-computed, already-paid-for answer: the
    reply task dies, the outbox parks the row, and the user is left unable to
    tell "still thinking" from "gone" — so they re-ask and buy the turn twice.

    Retries are bounded and only for classes that can plausibly succeed later.
    All attempts share one durable ``delivery_id`` so the outbox keeps a single
    row per reply whose final state is the true outcome, rather than one row
    per attempt.

    Returns ``None`` on delivery, else the taxonomy class of the last failure.
    """
    metadata = dict(message.metadata or {})
    metadata.setdefault("delivery_id", uuid.uuid4().hex)
    message = message.model_copy(update={"metadata": metadata})

    last_class = UNCLASSIFIED_ERROR_CLASS
    for attempt in range(_REPLY_SEND_ATTEMPTS):
        try:
            await channel.send(message)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - provider boundary
            last_class = classify_channel_send_error(exc)
            retryable = last_class in REQUIRED_RETRYABLE_ERROR_CLASSES
            final = attempt == _REPLY_SEND_ATTEMPTS - 1
            log.warning(
                "channel_dispatch.reply_send_failed",
                session_key=session_key,
                error_class=last_class,
                error_type=type(exc).__name__,
                attempt=attempt,
                will_retry=retryable and not final,
            )
            if not retryable or final:
                return last_class
            await asyncio.sleep(_reply_retry_delay(exc, attempt))
            continue
        else:
            return None
    return last_class


async def _notify_channel_reply_lost(
    channel: Any,
    *,
    route_envelope: Any,
    session_key: str,
    error_class: str,
) -> None:
    """Tell the user their answer exists but could not be delivered.

    Best-effort by construction: this is itself a send on a channel that just
    failed. It is skipped where every send to the target fails anyway, and it
    never raises — a failed notice must not replace the failure it reports.
    """
    if error_class in _REPLY_NOTICE_HOPELESS_CLASSES:
        return
    try:
        await channel.send(
            _route_envelope_reply_message(
                "I finished this reply but could not deliver it to this chat. "
                "Ask again to have it re-sent, or check the gateway's channel "
                "diagnostics if it keeps happening.",
                route_envelope,
                metadata={"delivery_failure_notice": True, "error_class": error_class},
            )
        )
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 - best effort by design
        log.warning(
            "channel_dispatch.reply_failure_notice_failed",
            session_key=session_key,
            error_class=error_class,
            error_type=type(exc).__name__,
        )


def _plan_outbound_pieces(channel: Any, message: OutgoingMessage) -> list[OutgoingMessage]:
    """Split a reply to fit the channel's declared length cap.

    A reply over a platform's per-message cap is rejected wholesale, and six
    adapters declare no cap and pass content straight through. This is the one
    place that enforces the capability contract's length budget centrally, in
    the unit the platform counts in. Adapters that split inside their own
    ``send()`` opt out via ``splits_natively`` and are returned unchanged.

    Chunking is preferred; a single unsplittable unit that still overflows is
    truncated with a footer — delivered-but-clipped beats platform-rejected.
    """
    profile = channel_capability_profile(channel)
    if profile is None or profile.max_message_len <= 0 or profile.splits_natively:
        return [message]
    unit = profile.length_unit
    limit = profile.max_message_len
    content = message.content or ""
    if measured_len(content, unit) <= limit:
        return [message]

    pieces: list[OutgoingMessage] = []
    for idx, chunk in enumerate(split_text_for_channel(content, limit, unit=unit)):
        if measured_len(chunk, unit) > limit:
            chunk = truncate_to_limit(chunk, limit, unit=unit)
        pieces.append(_as_chunk_message(message, chunk, first=idx == 0))
    return pieces


def _as_chunk_message(message: OutgoingMessage, chunk: str, *, first: bool) -> OutgoingMessage:
    """A one-chunk copy of ``message`` with a fresh outbox identity.

    The delivery id is dropped so the outbox mints a distinct one per chunk —
    otherwise ``begin_send``'s INSERT-OR-IGNORE collapses every chunk into a
    single row and the last receipt wins. Attachments ride only the first
    chunk so a long reply's files are not re-sent per piece.
    """
    metadata = dict(message.metadata or {})
    metadata.pop("delivery_id", None)
    return message.model_copy(
        update={
            "content": chunk,
            "metadata": metadata,
            "attachments": message.attachments if first else [],
        }
    )


async def _deliver_reply_or_notify(
    channel: Any,
    message: OutgoingMessage,
    *,
    route_envelope: Any,
    session_key: str,
) -> bool:
    """Send a reply; on final failure tell the user rather than going silent."""
    error_class: str | None = None
    for piece in _plan_outbound_pieces(channel, message):
        error_class = await _send_channel_reply_guarded(channel, piece, session_key=session_key)
        if error_class is not None:
            break
    if error_class is None:
        return True
    log.error(
        "channel_dispatch.reply_undelivered",
        session_key=session_key,
        error_class=error_class,
    )
    await _notify_channel_reply_lost(
        channel,
        route_envelope=route_envelope,
        session_key=session_key,
        error_class=error_class,
    )
    return False


async def _deliver_runtime_channel_reply(
    *,
    channel: Any,
    task_runtime: Any,
    session_manager: Any,
    session_key: str,
    task_id: str,
    route_envelope: Any,
    inbound: IncomingMessage,
    transcript_watermark: int,
    replayed: bool = False,
    config: Any = None,
    stream_relay: _RuntimeChannelStreamRelay | None = None,
) -> None:
    """Await a task_runtime result and send the channel reply.

    ``stream_relay.close()`` is always called in the ``finally`` block so that
    the streaming task is properly terminated even when this coroutine is
    cancelled or raises an unexpected exception (pitfall d).
    """
    wait = getattr(task_runtime, "wait", None)
    if not callable(wait):
        raise RuntimeError("task runtime does not support wait()")

    record = None
    wait_exc: Exception | None = None
    try:
        record = await wait(task_id)
    except Exception as exc:
        wait_exc = exc
        log.warning("channel_dispatch.runtime_wait_failed", session_key=session_key, exc_info=True)
    finally:
        if stream_relay is not None:
            await stream_relay.close()

    if wait_exc is not None:
        await channel.send(
            _build_runtime_reply_message(
                channel,
                build_terminal_reply(_terminal_payload_from_exception(wait_exc)),
                inbound,
                route_envelope,
            )
        )
        return

    status = _status_value(getattr(record, "status", None))
    if status == "succeeded":
        exact_content = await _replayed_assistant_text(
            session_manager,
            session_key,
            record,
        )
        if exact_content is not None:
            content = exact_content
        elif replayed:
            content = "The task completed, but its original channel reply could not be recovered."
        else:
            # Compatibility for tasks created before exact channel output was
            # persisted in task details. Replays never use this heuristic.
            content = await _latest_assistant_text_after(
                session_manager,
                session_key,
                transcript_watermark,
            )
        if (
            stream_relay is not None
            and stream_relay.stream_error is None
            and (content or stream_relay.has_terminal_snapshot)
        ):
            canonical_content, canonical_artifacts = _split_assistant_artifact_content(content)
            if stream_relay.delivered_artifact_keys:
                canonical_artifacts = [
                    artifact
                    for artifact in canonical_artifacts
                    if _artifact_delivery_key(artifact) not in stream_relay.delivered_artifact_keys
                ]
            canonical_content = _strip_artifact_markers_from_channel_text(canonical_content)
            canonical_content = _strip_delivered_artifact_image_references(
                canonical_content,
                canonical_artifacts,
            )
            if not _can_deliver_channel_files(channel):
                fallback_lines = _artifact_fallback_lines(canonical_artifacts)
                if fallback_lines:
                    canonical_content = "\n\n".join(
                        part for part in (canonical_content, "\n".join(fallback_lines)) if part
                    )
            if await stream_relay.reconcile_final_text(canonical_content):
                return
            stream_relay.stream_error = RuntimeError(
                "streamed channel reply could not apply persisted terminal text"
            )
        elif (
            stream_relay is not None
            and stream_relay.text_emitted
            and stream_relay.stream_error is None
        ):
            return
    else:
        if (
            stream_relay is not None
            and stream_relay.has_terminal_generation_reset
            and stream_relay.text_emitted
            and stream_relay.stream_error is None
        ):
            # The reset snapshot is the one visible terminal outcome.  close()
            # has already reconciled any speculative preview to that value.
            return
        content = build_terminal_reply(record)
        if (
            stream_relay is not None
            and stream_relay.text_emitted
            and stream_relay.stream_error is None
        ):
            content = _terminal_reply_suffix(content)

    if content:
        content, artifacts = _split_assistant_artifact_content(content)
        if stream_relay is not None and stream_relay.delivered_artifact_keys:
            artifacts = [
                artifact
                for artifact in artifacts
                if _artifact_delivery_key(artifact) not in stream_relay.delivered_artifact_keys
            ]
        content = _strip_artifact_markers_from_channel_text(content)
        content = _strip_delivered_artifact_image_references(content, artifacts)
        if stream_relay is not None and stream_relay.attempted_artifact_keys:
            artifacts = [
                artifact
                for artifact in artifacts
                if not stream_relay.attempted_artifact(artifact)
            ]
        if _can_deliver_channel_files(channel):
            if content:
                await _deliver_reply_or_notify(
                    channel,
                    _build_runtime_reply_message(
                        channel,
                        content,
                        inbound,
                        route_envelope,
                    ),
                    route_envelope=route_envelope,
                    session_key=session_key,
                )
            undelivered = await _deliver_artifacts_as_channel_files(
                channel,
                inbound,
                artifacts,
                config,
            )
            fallback_lines = _artifact_fallback_lines(undelivered)
            if fallback_lines:
                await _deliver_reply_or_notify(
                    channel,
                    _build_runtime_reply_message(
                        channel,
                        "\n".join(fallback_lines),
                        inbound,
                        route_envelope,
                    ),
                    route_envelope=route_envelope,
                    session_key=session_key,
                )
        else:
            fallback_lines = _artifact_fallback_lines(artifacts)
            if fallback_lines:
                content = "\n\n".join(part for part in (content, "\n".join(fallback_lines)) if part)
            if content:
                await _deliver_reply_or_notify(
                    channel,
                    _build_runtime_reply_message(
                        channel,
                        content,
                        inbound,
                        route_envelope,
                    ),
                    route_envelope=route_envelope,
                    session_key=session_key,
                )


def _split_assistant_artifact_content(content: str) -> tuple[str, list[dict[str, Any]]]:
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content, []
    if not isinstance(parsed, dict):
        return content, []
    text = parsed.get("text")
    artifacts_raw = parsed.get("artifacts")
    if not isinstance(text, str) or not isinstance(artifacts_raw, list):
        return content, []
    artifacts: list[dict[str, Any]] = []
    for artifact in artifacts_raw:
        try:
            payload = artifact_payload(artifact)
        except Exception:
            continue
        if payload:
            artifacts.append(payload)
    return text, artifacts


async def _run_turn_batch_path(
    channel: Any,
    turn_runner: Any,
    msg: IncomingMessage,
    session_key: str,
    tool_ctx: Any,
    event_bridge: EventBridge | None,
    semantic_message: str | None,
    config: Any,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    """Batch mode: accumulate all text, send once at the end."""
    text_parts: list[str] = []
    done_snapshot_present = False
    done_snapshot_text = ""
    artifacts: list[dict[str, Any]] = []
    error_occurred = False
    terminal_generation_reset_text: str | None = None
    clarify_card_sent = False

    run_kwargs: dict[str, Any] = {
        "tool_context": tool_ctx,
        "agent_id": tool_ctx.agent_id,
    }
    model = resolve_agent_model(tool_ctx.agent_id, config)
    if model is not None and _accepts_keyword_arg(turn_runner.run, "model"):
        run_kwargs["model"] = model
    if _accepts_keyword_arg(turn_runner.run, "semantic_message"):
        run_kwargs["semantic_message"] = semantic_message
    if attachments and _accepts_keyword_arg(turn_runner.run, "attachments"):
        run_kwargs["attachments"] = attachments
    try:
        stream = turn_runner.run(
            msg.content,
            session_key,
            **run_kwargs,
        )
        from opensquilla.engine.stream_wrappers import is_context_bound_owner

        async for event in _wrap_channel_turn_stream(
            stream,
            config,
            context_bound=is_context_bound_owner(turn_runner),
        ):
            if isinstance(event, TextDeltaEvent):
                if clarify_card_sent:
                    continue
                text_parts.append(event.text)
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.text_delta",
                        {
                            "text": event.text,
                            "presentation": getattr(event, "presentation", "answer"),
                        },
                    )
            elif isinstance(event, DoneEvent):
                snapshot_present, snapshot_text = done_text_snapshot(event)
                if snapshot_present:
                    done_snapshot_present = True
                    done_snapshot_text = snapshot_text
            elif isinstance(event, AnswerGenerationResetEvent):
                await _emit_generation_reset(event_bridge, session_key, event)
                text_parts.clear()
                done_snapshot_present = bool(event.authoritative_text_snapshot)
                done_snapshot_text = event.authoritative_text_snapshot
                if event.terminal:
                    log.error(
                        "channel_dispatch.agent_terminal_generation_reset",
                        session_key=session_key,
                        failure_kind=event.terminal_failure_kind or None,
                    )
                    terminal_generation_reset_text = _terminal_generation_reset_text(
                        event
                    )
                    error_occurred = True
                    # Keep draining the shared TurnRunner. Its accounting-only
                    # Done is hidden from this public stream, but reaching EOF
                    # is what lets the finalizer persist usage and the error.
                    continue
            elif artifact := _artifact_event_payload(event):
                artifacts.append(artifact)
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.artifact",
                        artifact,
                    )
            elif isinstance(event, RunHeartbeatEvent):
                await _emit_run_heartbeat(event_bridge, session_key, event)
            elif isinstance(event, RouterDecisionEvent):
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.router_decision",
                        _router_decision_payload(event),
                    )
            elif isinstance(event, EnsembleProgressEvent):
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.ensemble_progress",
                        _ensemble_progress_payload(event),
                    )
            elif isinstance(event, ToolUseStartEvent):
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.tool_use_start",
                        _tool_use_start_payload(event),
                    )
            elif isinstance(event, ToolUseDeltaEvent):
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.tool_use_delta",
                        _tool_use_delta_payload(event),
                    )
            elif isinstance(event, ToolUseEndEvent):
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.tool_use_end",
                        _tool_use_end_payload(event),
                    )
            elif isinstance(event, ToolResultEvent):
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.tool_result",
                        _tool_result_payload(event),
                    )
                if await _maybe_send_clarify_channel_card(channel, msg, event):
                    clarify_card_sent = True
            elif isinstance(event, ErrorEvent):
                if terminal_generation_reset_text is not None:
                    continue
                log.error(
                    "channel_dispatch.agent_error",
                    session_key=session_key,
                    failure_kind=event.failure_kind or None,
                    error_id=event.error_id or None,
                )
                await channel.send(
                    _build_reply_message(
                        channel,
                        append_error_ref(
                            build_terminal_reply(_terminal_payload_from_error_event(event)),
                            getattr(event, "error_id", "") or None,
                        ),
                        msg,
                    )
                )
                text_parts.clear()
                error_occurred = True
                break
    except TimeoutError as exc:
        log.error("channel_dispatch.agent_stream_timeout", session_key=session_key)
        if terminal_generation_reset_text is None:
            await channel.send(
                _build_reply_message(
                    channel,
                    build_terminal_reply(_terminal_payload_from_exception(exc)),
                    msg,
                )
            )
        text_parts.clear()
        error_occurred = True

    if terminal_generation_reset_text is not None:
        await channel.send(
            _build_reply_message(channel, terminal_generation_reset_text, msg)
        )
    elif not error_occurred:
        content = done_snapshot_text if done_snapshot_present else "".join(text_parts)
        content = _strip_artifact_markers_from_channel_text(content)
        content = _strip_delivered_artifact_image_references(content, artifacts)
        if _can_deliver_channel_files(channel):
            if content:
                await channel.send(_build_reply_message(channel, content, msg))
            undelivered = await _deliver_artifacts_as_channel_files(channel, msg, artifacts, config)
            artifact_lines = _artifact_fallback_lines(undelivered)
            if artifact_lines:
                await channel.send(_build_reply_message(channel, "\n".join(artifact_lines), msg))
        else:
            artifact_lines = _artifact_fallback_lines(artifacts)
            if artifact_lines:
                content = "\n\n".join(part for part in (content, "\n".join(artifact_lines)) if part)
            if content:
                await channel.send(_build_reply_message(channel, content, msg))


async def _run_turn_streaming_path(
    channel: Any,
    turn_runner: Any,
    msg: IncomingMessage,
    session_key: str,
    tool_ctx: Any,
    event_bridge: EventBridge | None,
    semantic_message: str | None,
    config: Any,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    """Streaming mode: feed text deltas through an async queue to send_streaming.

    Uses a queue + consumer task pattern so the turn runner and the
    channel streamer run concurrently.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    live_preview = _channel_can_replace_streamed_text(channel)
    text_emitted = False
    text_parts: list[str] = []
    done_snapshot_present = False
    done_snapshot_text = ""
    stream_error: str | None = None
    terminal_generation_reset = False
    stream_task_error: BaseException | None = None
    stream_handle: _StreamedMessageHandle | None = None
    terminal_reconcile_fallback = ""
    yielded_stream_chunks: list[str] = []
    stream_delivered_index = 0
    artifacts: list[dict[str, Any]] = []
    stream_sanitizer = _DirectiveTagStreamSanitizer()
    clarify_card_sent = False

    async def _chunk_iter() -> AsyncIterator[str]:
        """Async iterator that yields text chunks from the queue."""
        nonlocal stream_delivered_index
        while True:
            chunk = await queue.get()
            if chunk is None:
                tail = stream_sanitizer.flush()
                if tail:
                    yielded_stream_chunks.append(tail)
                    yield tail
                    stream_delivered_index = len(yielded_stream_chunks)
                return
            cleaned = stream_sanitizer.clean(chunk)
            if cleaned:
                yielded_stream_chunks.append(cleaned)
                yield cleaned
                stream_delivered_index = len(yielded_stream_chunks)

    # Start the streaming consumer as a background task
    streaming_reply_kwargs = _streaming_reply_kwargs(channel, msg)
    stream_task = asyncio.create_task(
        channel.send_streaming(
            _chunk_iter(),
            **streaming_reply_kwargs,
        ),
    )

    try:
        run_kwargs: dict[str, Any] = {
            "tool_context": tool_ctx,
            "agent_id": tool_ctx.agent_id,
        }
        model = resolve_agent_model(tool_ctx.agent_id, config)
        if model is not None and _accepts_keyword_arg(turn_runner.run, "model"):
            run_kwargs["model"] = model
        if _accepts_keyword_arg(turn_runner.run, "semantic_message"):
            run_kwargs["semantic_message"] = semantic_message
        if attachments and _accepts_keyword_arg(turn_runner.run, "attachments"):
            run_kwargs["attachments"] = attachments
        stream = turn_runner.run(
            msg.content,
            session_key,
            **run_kwargs,
        )
        from opensquilla.engine.stream_wrappers import is_context_bound_owner

        async for event in _wrap_channel_turn_stream(
            stream,
            config,
            context_bound=is_context_bound_owner(turn_runner),
        ):
            if isinstance(event, TextDeltaEvent):
                if clarify_card_sent:
                    continue
                cleaned = _strip_artifact_markers_from_channel_text(event.text)
                if cleaned:
                    text_emitted = True
                    text_parts.append(cleaned)
                    if live_preview:
                        await queue.put(cleaned)
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.text_delta",
                        {
                            "text": event.text,
                            "presentation": getattr(event, "presentation", "answer"),
                        },
                    )
            elif isinstance(event, DoneEvent):
                snapshot_present, snapshot_text = done_text_snapshot(event)
                if snapshot_present:
                    done_snapshot_present = True
                    done_snapshot_text = snapshot_text
            elif isinstance(event, AnswerGenerationResetEvent):
                await _emit_generation_reset(event_bridge, session_key, event)
                text_parts.clear()
                done_snapshot_present = bool(event.authoritative_text_snapshot)
                done_snapshot_text = event.authoritative_text_snapshot
                if event.terminal:
                    log.error(
                        "channel_dispatch.agent_terminal_generation_reset",
                        session_key=session_key,
                        failure_kind=event.terminal_failure_kind or None,
                    )
                    terminal_generation_reset = True
                    done_snapshot_present = True
                    done_snapshot_text = _terminal_generation_reset_text(event)
                    # Drain through finalization before closing the wrapper;
                    # stopping here can cancel DB/accounting work in progress.
                    continue
            elif artifact := _artifact_event_payload(event):
                artifacts.append(artifact)
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.artifact",
                        artifact,
                    )
            elif isinstance(event, RunHeartbeatEvent):
                await _emit_run_heartbeat(event_bridge, session_key, event)
            elif isinstance(event, RouterDecisionEvent):
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.router_decision",
                        _router_decision_payload(event),
                    )
            elif isinstance(event, EnsembleProgressEvent):
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.ensemble_progress",
                        _ensemble_progress_payload(event),
                    )
            elif isinstance(event, ToolUseStartEvent):
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.tool_use_start",
                        _tool_use_start_payload(event),
                    )
            elif isinstance(event, ToolUseDeltaEvent):
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.tool_use_delta",
                        _tool_use_delta_payload(event),
                    )
            elif isinstance(event, ToolUseEndEvent):
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.tool_use_end",
                        _tool_use_end_payload(event),
                    )
            elif isinstance(event, ToolResultEvent):
                if event_bridge is not None:
                    await event_bridge.emit(
                        session_key,
                        "session.event.tool_result",
                        _tool_result_payload(event),
                    )
                if await _maybe_send_clarify_channel_card(channel, msg, event):
                    clarify_card_sent = True
            elif isinstance(event, ErrorEvent):
                if terminal_generation_reset:
                    continue
                log.error(
                    "channel_dispatch.agent_error",
                    session_key=session_key,
                    failure_kind=event.failure_kind or None,
                    error_id=event.error_id or None,
                )
                stream_error = append_error_ref(
                    build_terminal_reply(_terminal_payload_from_error_event(event)),
                    getattr(event, "error_id", "") or None,
                )
                break
    except TimeoutError as exc:
        log.error("channel_dispatch.agent_stream_timeout", session_key=session_key)
        if not terminal_generation_reset:
            stream_error = build_terminal_reply(_terminal_payload_from_exception(exc))
    finally:
        if not live_preview:
            terminal_text = done_snapshot_text if done_snapshot_present else "".join(text_parts)
            if terminal_text:
                await queue.put(terminal_text)
        # Signal end-of-stream to the consumer
        await queue.put(None)
        # Wait for the streaming task to finish
        try:
            stream_result = await asyncio.wait_for(stream_task, timeout=10.0)
            stream_handle = _streamed_message_handle(
                stream_result,
                streaming_reply_kwargs,
            )
        except TimeoutError as exc:
            stream_task_error = exc
            log.warning(
                "channel_dispatch.direct_streaming_failed",
                channel_type=type(channel).__name__,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            stream_task.cancel()
        except Exception as exc:  # noqa: BLE001 - streaming adapter fallback below
            stream_task_error = exc
            log.warning(
                "channel_dispatch.direct_streaming_failed",
                channel_type=type(channel).__name__,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            stream_task.cancel()

    if (
        stream_task_error is None
        and live_preview
        and done_snapshot_present
        and stream_error is None
    ):
        canonical_text = _strip_delivered_artifact_image_references(
            done_snapshot_text,
            artifacts,
        )
        canonical_text = _sanitize_streamed_channel_text(canonical_text)
        streamed_text = "".join(yielded_stream_chunks)
        if canonical_text != streamed_text:
            if await _replace_streamed_channel_text(
                channel,
                stream_handle,
                canonical_text,
            ):
                yielded_stream_chunks[:] = [canonical_text] if canonical_text else []
                stream_delivered_index = len(yielded_stream_chunks)
                text_emitted = bool(canonical_text)
            else:
                stream_task_error = RuntimeError(
                    "streamed channel reply could not apply terminal text snapshot"
                )
                terminal_reconcile_fallback = canonical_text

    if stream_task_error is not None and text_emitted:
        queued_remainder: list[str] = []
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, str):
                cleaned = stream_sanitizer.clean(item)
                if cleaned:
                    queued_remainder.append(cleaned)
        tail = stream_sanitizer.flush()
        if tail:
            queued_remainder.append(tail)
        undelivered_yielded = "".join(yielded_stream_chunks[stream_delivered_index:])
        fallback_text = terminal_reconcile_fallback or undelivered_yielded + "".join(
            queued_remainder
        )
        if fallback_text:
            try:
                await channel.send(_build_reply_message(channel, fallback_text, msg))
            except Exception as exc:  # noqa: BLE001 - best-effort fallback
                log.warning(
                    "channel_dispatch.direct_streaming_batch_fallback_failed",
                    channel_type=type(channel).__name__,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    # Error recovery
    if stream_error is not None:
        if text_emitted:
            # Mid-stream: edit the existing message to append error
            try:
                await channel.send(
                    _build_reply_message(channel, _terminal_reply_suffix(stream_error), msg),
                )
            except Exception:
                pass  # best-effort error append
        else:
            # Pre-stream: standalone error message
            await channel.send(
                _build_reply_message(channel, stream_error, msg),
            )
    elif artifacts and not terminal_generation_reset:
        if _can_deliver_channel_files(channel):
            undelivered = await _deliver_artifacts_as_channel_files(channel, msg, artifacts, config)
        else:
            undelivered = artifacts
        fallback_lines = _artifact_fallback_lines(undelivered)
        if fallback_lines:
            await channel.send(
                _build_reply_message(channel, "\n".join(fallback_lines), msg),
            )


# ── Gap 5: Event emission ────────────────────────────────────────────────


async def _emit_events(
    event_bridge: EventBridge,
    session_key: str,
    reason: str,
) -> None:
    """Broadcast session events to WebSocket subscribers.

    Placeholder: emits ``sessions.changed`` with the given reason.
    A richer implementation will follow once the EventBridge is created.
    """
    await event_bridge.emit(
        session_key,
        "sessions.changed",
        build_sessions_changed_payload(session_key, reason),
    )
