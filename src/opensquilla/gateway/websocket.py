"""WebSocket connection handler: handshake, frame parsing, event loop."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from opensquilla import __version__
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import (
    GatewayConfig,
    effective_agent_stream_idle_timeout_seconds,
    effective_webui_stream_idle_grace_seconds,
)
from opensquilla.gateway.origin_guard import websocket_origin_allowed
from opensquilla.gateway.protocol import (
    ERROR_UNAVAILABLE,
    MAX_PAYLOAD_BYTES,
    PREAUTH_TIMEOUT_MS,
    PROTOCOL_VERSION,
    WS_CLOSE_SERVICE_RESTART,
    HelloOk,
    PolicyInfo,
    ResFrame,
    ServerInfo,
    SnapshotInfo,
    make_error_res,
    make_event,
    make_ok_res,
    project_session_event_for_client,
)
from opensquilla.gateway.recovery_scheduler import (
    CURRENT_RECOVERY_OPERATION,
    READ_BUDGET_SECONDS,
    RECOVERY_CAPABILITY,
    RecoveryOperation,
    get_recovery_scheduler,
)
from opensquilla.gateway.rpc import RpcContext, RpcDispatcher
from opensquilla.gateway.rpc.ingress import (
    RpcIngressValidationError,
    is_utf8_encodable,
    validate_rpc_ingress,
)
from opensquilla.gateway.transport_flow import (
    CONNECTION_BUFFER_BYTES,
    CONTROL_BUFFER_BYTES,
    CONTROL_BUFFER_FRAMES,
    FLOW_CAPABILITY,
    FLOW_WINDOW_BYTES,
    FLOW_WINDOW_FRAMES,
    PROBE_CAPABILITY,
    BudgetKind,
    FlowWindow,
    get_transport_budget,
)
from opensquilla.sandbox.legacy_codec import encode_payload_for_protocol

if TYPE_CHECKING:
    from opensquilla.gateway.snapshot_transfer import SnapshotRegistry, SnapshotTransfer

log = structlog.get_logger(__name__)

_SnapshotIdentity = tuple[str | None, int | None]
_FlowInstallReceipt = tuple[Any, Any, Any, Any, Any]
_InstalledFlowReceipt = tuple[Any, Any, Any, Any, Any, _SnapshotIdentity]


# ---------------------------------------------------------------------------
# Outbound writer queue primitives
# ---------------------------------------------------------------------------
#
# When the per-connection writer queue is enabled, every outbound frame
# (events, RPC responses, ticks) is enqueued from any producer task and
# drained sequentially by a dedicated writer task. WS-frame ``seq`` is
# minted by the writer at DEQUEUE time so that lossy drops never consume
# a seq number — the Vue RPC client in ``opensquilla-webui/src/lib/rpc.ts``
# closes the socket on any seq gap.
#
# ``_LOSSY_EVENTS`` is intentionally narrow: the lossy event MUST NOT be
# routed through ``SessionStreamRegistry.record()`` upstream, otherwise a
# silent drop here would create a ``stream_seq`` gap that could be rejected by
# the Vue client's ``utils/chat/streamEvents.ts:acceptStreamSeq`` reconciliation
# on reconnect. The only event that satisfies that constraint today is the
# liveness ``tick`` emitted from ``_tick_loop`` — its name is not prefixed
# ``session.event.`` so ``EventBridge.emit`` skips ``record()`` for it.
# Any future addition to this set MUST be verified against the same
# upstream invariant.
_LOSSY_EVENTS: frozenset[str] = frozenset({"tick"})
_DETACHED_READ_METHODS: frozenset[str] = frozenset({"chat.history"})
_MAX_DETACHED_READS_PER_CONNECTION = 4
_DETACHED_READ_STOP_TIMEOUT_SECONDS = 2.0
_DIRECT_SEND_TIMEOUT_SECONDS = 2.0
_DIRECT_CLOSE_TIMEOUT_SECONDS = 1.0
_MAX_ORDINARY_REQUESTS = 8
_ORDINARY_DRAIN_SECONDS = 0.25
_CONTROL_RPC_METHODS = frozenset({"transport.flow.update"})
_RECOVERY_READ_METHODS = frozenset({
    "chat.history", "sessions.messages.subscribe", "sessions.messages.snapshot",
    "sessions.messages.snapshot.read", "sessions.messages.hydrate", "sessions.messages.resume",
})
_RECOVERY_CONTROL_METHODS = frozenset({
    "sessions.messages.unsubscribe", "sessions.messages.snapshot.release",
})
_SESSION_MUTATION_METHODS = frozenset({
    "chat.send", "sessions.send", "sessions.steer.v2", "sessions.reset", "sessions.delete",
    "sessions.truncate", "sessions.contextCompact", "sessions.pending_inputs.enqueue",
    "sessions.pending_inputs.dispatch", "sessions.pending_inputs.steer",
})
_SESSION_IDENTITY_MUTATIONS = frozenset({
    "sessions.reset", "sessions.delete", "sessions.truncate", "sessions.contextCompact",
})
# Running mutations retain their existing completion semantics after a peer
# leaves. Strong references keep their result supervised until completion.
_DRAINING_ORDINARY_WORKERS: set[asyncio.Task[None]] = set()
_ORDINARY_WORKERS: set[asyncio.Task[None]] = set()
_MAX_ORDINARY_WORKERS = 128
_WRITER_TASKS: set[asyncio.Task[None]] = set()
_DRAINING_WRITER_TASKS: set[asyncio.Task[None]] = set()
_SOCKET_CLOSE_TASKS: set[asyncio.Task[None]] = set()
_MAX_WRITER_TASKS = 128
_WRITER_STOP_SECONDS = 2.0

# Sentinel pushed into the outbox by ``_stop_writer`` to wake a writer
# blocked in ``await self._outbox.get()`` and exit cleanly.
_SENTINEL_STOP: Any = object()
# Keep this list side-effect free: the Web UI uses the advertised methods to
# turn a reconnect-on-timeout fallback into a request-local rejection.
_CONCURRENT_OPTIONAL_READ_METHODS: frozenset[str] = frozenset(
    {
        "agents.list",
        "artifacts.list",
        "commands.list_for_surface",
        "config.get",
        "models.routing.get",
        "onboarding.status",
        "sandbox.run_mode.preference.get",
        "sessions.list",
        "sessions.messages.hydrate",
        "usage.status",
        "workspaces.list",
    }
)
_CANCELLABLE_REQUEST_METHODS: frozenset[str] = frozenset(
    {
        "onboarding.llmProfile.draft.probe",
        "onboarding.llmProfile.probe",
        "onboarding.provider.probe",
    }
)
_PROVIDER_PROBE_MODES: tuple[str, ...] = ("model", "reachability")
_ACTIVE_PROVIDER_PROBE_LEASES: set[object] = set()
_MAX_ACTIVE_PROVIDER_PROBES = 16
_BASE_DETACHED_RPC_METHODS: frozenset[str] = frozenset(
    {"meta.drafts.list", "skills.install"}
).union(
    _CONCURRENT_OPTIONAL_READ_METHODS,
)
_DETACHED_RPC_METHODS: frozenset[str] = _BASE_DETACHED_RPC_METHODS.union(
    _CANCELLABLE_REQUEST_METHODS,
)
# Reserve one bounded slot for every method that may legitimately run detached.
# A fresh WebUI can issue every optional metadata read plus draft recovery before
# the first responses arrive; keeping this derived from the allowlist prevents a
# newly advertised read from silently outgrowing the bootstrap budget again.
_MAX_DETACHED_REQUESTS_PER_CONNECTION = len(_BASE_DETACHED_RPC_METHODS)
_MAX_CANCELLABLE_REQUESTS_PER_CONNECTION = len(_CANCELLABLE_REQUEST_METHODS)
_DETACHED_REQUEST_DRAIN_SECONDS = 0.25


def _should_detach_rpc_request(method: str, params: Any) -> bool:
    if method not in _DETACHED_RPC_METHODS:
        return False
    if method in _CANCELLABLE_REQUEST_METHODS:
        return _is_cancellable_request(method, params)
    if method != "skills.install":
        return True
    if not isinstance(params, dict):
        return False
    return any(
        isinstance(params.get(key), str) and bool(params[key].strip())
        for key in ("operationId", "operation_id")
    )


def _is_cancellable_request(method: str, params: Any) -> bool:
    if method not in _CANCELLABLE_REQUEST_METHODS or not isinstance(params, dict):
        return False
    mode = params.get("mode")
    return isinstance(mode, str) and mode.strip() in {"model", "reachability"}


def _active_provider_probe_cleanup_tasks() -> int:
    # Import lazily so ordinary Gateway startup does not load provider setup
    # machinery solely to report a transport capability.
    from opensquilla.onboarding.probe import active_provider_probe_cleanup_tasks

    return active_provider_probe_cleanup_tasks()


def _is_provider_probe_request(method: str) -> bool:
    return method in _CANCELLABLE_REQUEST_METHODS


def _try_acquire_provider_probe_lease() -> object | None:
    if (
        len(_ACTIVE_PROVIDER_PROBE_LEASES) + _active_provider_probe_cleanup_tasks()
        >= _MAX_ACTIVE_PROVIDER_PROBES
    ):
        return None
    lease = object()
    _ACTIVE_PROVIDER_PROBE_LEASES.add(lease)
    return lease


def _release_provider_probe_lease(lease: object) -> None:
    _ACTIVE_PROVIDER_PROBE_LEASES.discard(lease)


@dataclass(slots=True)
class _OutboundFrame:
    """A frame queued for the writer task.

    ``seq`` is deliberately absent — it is minted by ``_writer_loop`` at
    dequeue time. ``kind`` is used by same-kind eviction; for events it is
    ``f"event:{event_name}"``, for RPC responses it is ``"res"``, and raw
    protocol frames such as pong use ``"raw"``.
    """

    kind: str
    classification: str  # "lossy" or "control"
    payload: Any
    event_name: str | None
    res_frame: ResFrame | None
    meta: dict[str, Any] | None = None
    raw_text: str | None = None
    encoded_text: str | None = None
    budget_bytes: int = 0
    delivery_id: int | None = None
    is_control: bool = False
    budget_kind: BudgetKind = None


@dataclass(eq=False, slots=True)
class MessageSubscriptionIntent:
    token: str
    ready: asyncio.Future[bool]
    closed: bool = False

    def retire(self) -> None:
        self.closed = True
        if not self.ready.done():
            self.ready.set_result(False)


def _payload_field(payload: Any, key: str) -> Any:
    """Best-effort extraction of a field from a payload dict; tolerates non-dicts."""
    if isinstance(payload, dict):
        return payload.get(key)
    return None


@dataclass
class WsConnection:
    """Represents a connected WebSocket client."""

    conn_id: str
    ws: WebSocket
    protocol: int = PROTOCOL_VERSION
    client_caps: frozenset[str] = field(default_factory=frozenset)
    principal: Principal = field(
        default_factory=lambda: Principal(
            role="operator",
            scopes=frozenset(["operator.admin"]),
            is_owner=True,
            authenticated=False,
        )
    )
    connected_at: int = field(default_factory=lambda: int(time.time() * 1000))
    _seq: int = field(default=0, init=False)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    # Writer-queue state.
    # ``_queue_enabled`` mirrors the kill-switch config at registration time;
    # once a connection starts in legacy mode it stays in legacy mode for life.
    _queue_enabled: bool = field(default=False, init=False, repr=False)
    _writer_queue_maxsize: int = field(default=512, init=False, repr=False)
    _outbox: asyncio.Queue[Any] | None = field(default=None, init=False, repr=False)
    _writer_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _detached_read_tasks: set[asyncio.Task[None]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    _closing: bool = field(default=False, init=False, repr=False)
    _detached_request_tasks: set[asyncio.Task[None]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    _cancellable_request_tasks: dict[str, asyncio.Task[None]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _cancelled_request_tasks: set[asyncio.Task[None]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    _accept_detached_responses: bool = field(default=True, init=False, repr=False)
    _ordinary_queue: (
        asyncio.Queue[
            tuple[Coroutine[Any, Any, None], int, object | None, asyncio.Future[None] | None]
        ]
        | None
    ) = field(default=None, init=False, repr=False)
    _ordinary_worker: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _ordinary_stopped: bool = field(default=False, init=False, repr=False)
    _transport_bytes: int = field(default=0, init=False, repr=False)
    _transport_cleanup: list[Callable[[], Any]] = field(
        default_factory=list, init=False, repr=False
    )
    _flow: FlowWindow | None = field(default=None, init=False, repr=False)
    _flow_control_frames: int = field(default=0, init=False, repr=False)
    _flow_control_bytes: int = field(default=0, init=False, repr=False)
    _flow_dirty_notice_pending: _OutboundFrame | None = field(default=None, init=False, repr=False)
    _flow_snapshot_delivery: int | None = field(default=None, init=False, repr=False)
    _subscriptions: Any = field(default=None, init=False, repr=False)
    _snapshot_transfer: SnapshotTransfer | None = field(default=None, init=False, repr=False)
    _snapshot_registry: SnapshotRegistry | None = field(default=None, init=False, repr=False)
    _recovery_operations: dict[str, RecoveryOperation] = field(default_factory=dict, init=False)
    _recovery_enabled: bool = field(default=False, init=False, repr=False)
    _delivery_timers: dict[int, asyncio.TimerHandle] = field(default_factory=dict, init=False)
    _transport_kinds: dict[str, int] = field(default_factory=dict, init=False)
    _recovery_runtime: object | None = field(default=None, init=False, repr=False)
    _resume_proofs: OrderedDict[str, dict[str, Any]] = field(
        default_factory=OrderedDict, init=False,
    )
    _flow_installed: OrderedDict[str, _InstalledFlowReceipt] = field(
        default_factory=OrderedDict, init=False, repr=False
    )

    @property
    def flow_enabled(self) -> bool:
        return self._flow is not None

    def _enable_flow(self) -> None:
        if self._flow is None:
            self._flow = FlowWindow(
                self.reserve_transport_bytes,
                self.release_transport_bytes,
                recovery_limit=2 if self._recovery_enabled else 1,
                reserve_recovery=lambda size: self.reserve_transport_bytes(size, kind="recovery"),
                release_recovery=lambda size: self.release_transport_bytes(size, kind="recovery"),
                on_stage=self._snapshot_piece_staged,
            )
            self.add_transport_cleanup(self._flow.close)

    def snapshot_registry(self) -> SnapshotRegistry:
        from opensquilla.gateway.snapshot_transfer import SnapshotRegistry

        if self._snapshot_registry is None:
            self._snapshot_registry = SnapshotRegistry(
                self.reserve_transport_bytes, self.release_transport_bytes,
            )
            self.add_transport_cleanup(self._snapshot_registry.close)
        return self._snapshot_registry

    def _snapshot_piece_staged(self, delivery: Any) -> None:
        if delivery.owner is not None and delivery.publication == "original":
            delivery.owner.stage_piece(delivery.segment_index)

    def _expire_snapshot_credit(self, delivery_id: int) -> None:
        self._delivery_timers.pop(delivery_id, None)
        if self._flow is None or self._closing:
            return
        delivery = self._flow.deliveries.get(delivery_id)
        if delivery is None or delivery.acknowledged:
            return
        self._closing = True
        task = asyncio.create_task(self._force_close(reason="recovery_credit_timeout", code=1013))
        task.add_done_callback(self._consume_task_result)

    def _prune_delivery_timers(self) -> None:
        for delivery_id, timer in tuple(self._delivery_timers.items()):
            delivery = self._flow.deliveries.get(delivery_id) if self._flow else None
            if delivery is None or delivery.acknowledged:
                timer.cancel()
                del self._delivery_timers[delivery_id]

    def reserve_snapshot_delivery(
        self, encoded_response_bytes: int, key: str, snapshot_id: str, sync_revision: str,
        *, segment_index: int = 0,
    ) -> dict[str, Any]:
        if self._flow is None:
            raise ValueError("Consumption feedback was not negotiated")
        transfer = (
            self.snapshot_registry().get(key, sync_revision, snapshot_id)
            if self._recovery_enabled else None
        )
        deadline = min(time.monotonic() + 30.0, transfer.deadline) if transfer else None
        delivery_id = self._flow.admit(
            encoded_response_bytes, recovery=True, key=key, owner=transfer,
            segment_index=segment_index, deadline=deadline,
        )
        if delivery_id is None:
            from opensquilla.gateway.snapshot_transfer import SnapshotTransferError

            raise SnapshotTransferError("SNAPSHOT_BUSY")
        self._flow_installed.pop(key, None)
        self._resume_proofs.pop(key, None)
        self._flow_snapshot_delivery = delivery_id
        if deadline is not None:
            self._delivery_timers[delivery_id] = asyncio.get_running_loop().call_later(
                max(0.0, deadline - time.monotonic()), self._expire_snapshot_credit, delivery_id,
            )
        return {"delivery_epoch": self._flow.epoch, "delivery_id": delivery_id}

    def cancel_snapshot_delivery(self, receipt: dict[str, Any]) -> None:
        # Preserve the cumulative ID with a tiny, explicit invalidation instead
        # of silently creating an ACK hole when a snapshot response fails.
        if self._flow is None or receipt.get("delivery_epoch") != self._flow.epoch:
            return
        delivery_id = receipt.get("delivery_id")
        if not isinstance(delivery_id, int) or isinstance(delivery_id, bool):
            return
        if delivery_id not in self._flow.deliveries:
            return
        if not self._recovery_enabled:
            self._closing = True
            task = asyncio.create_task(self._force_close(reason="snapshot_cancelled", code=1013))
            task.add_done_callback(self._consume_task_result)
            return
        if not self._flow.claim(delivery_id, "tombstone"):
            return
        text = make_event(
            "transport.flow.dirty",
            {"delivery_epoch": self._flow.epoch, "dirty_keys": [], "global_dirty": False},
            meta={"flow": receipt},
        ).model_dump_json(exclude={"seq"})
        frame = _OutboundFrame(
            kind="event:transport.flow.dirty",
            classification="control",
            payload=None,
            event_name="transport.flow.dirty",
            res_frame=None,
            encoded_text=text,
            delivery_id=delivery_id,
        )
        self._enqueue_frame(frame)

    def _mark_flow_dirty(self, payload: Any) -> None:
        assert self._flow is not None
        key = _payload_field(payload, "session_key") or _payload_field(payload, "key")
        generation = _payload_field(payload, "stream_generation")
        seq = _payload_field(payload, "stream_seq")
        was_global = self._flow.global_dirty
        changed = self._flow.mark_dirty(
            key if isinstance(key, str) else None,
            generation if isinstance(generation, str) else None,
            seq if isinstance(seq, int) and not isinstance(seq, bool) else 0,
        )
        if self._flow.global_dirty and self._subscriptions is not None:
            if (
                isinstance(key, str)
                and self._subscriptions.get_message_subscription_token(self.conn_id, key)
                is not None
            ):
                # A new loss on an already-reconciled lease invalidates that
                # lease, without forcing unrelated covered sessions to restart.
                self._subscriptions.set_message_flow_revision(self.conn_id, key, 0)
            elif was_global:
                # An unidentifiable additional loss cannot reuse a partially
                # fulfilled global revision as proof of recovery.
                changed = self._flow.mark_global_dirty(renew=True) or changed
        if changed:
            self._queue_flow_dirty_notice()

    def _flow_key_needs_global_recovery(self, key: Any) -> bool:
        return bool(
            self._flow is not None
            and self._flow.global_dirty
            and self._subscriptions is not None
            and isinstance(key, str)
            and self._subscriptions.get_message_subscription_token(self.conn_id, key) is not None
            and self._subscriptions.get_message_flow_revision(self.conn_id, key)
            != self._flow.global_revision
        )

    def _clear_covered_global_flow(self) -> None:
        if (
            self._flow is not None
            and self._flow.global_dirty
            and self._subscriptions is not None
            and self._subscriptions.all_message_flow_revisions_match(
                self.conn_id, self._flow.global_revision
            )
        ):
            self._flow.global_dirty = False

    def _retire_flow_subscription(self, key: str) -> None:
        self._resume_proofs.pop(key, None)
        current_operation = CURRENT_RECOVERY_OPERATION.get()
        for operation in tuple(self._recovery_operations.values()):
            if operation.key == key and operation is not current_operation:
                get_recovery_scheduler().cancel(operation)
        if self._flow is None:
            return
        self._flow.dirty.pop(key, None)
        self._flow_installed.pop(key, None)
        self._clear_covered_global_flow()

    def _queue_flow_dirty_notice(self) -> None:
        if self._flow is None or self._flow_dirty_notice_pending or self._closing:
            return
        frame = _OutboundFrame(
                kind="event:transport.flow.dirty",
                classification="control",
                # The writer refreshes the authoritative keys under a bounded
                # encoding budget; do not pre-encode the potentially long set.
                payload={
                    "delivery_epoch": self._flow.epoch,
                    "dirty_keys": [],
                    "global_dirty": True,
                },
                event_name="transport.flow.dirty",
                res_frame=None,
                is_control=True,
            )
        self._flow_dirty_notice_pending = frame
        self._enqueue_frame(frame)
        if self._closing and self._flow_dirty_notice_pending is frame:
            self._flow_dirty_notice_pending = None

    def _freeze_pending_dirty_notice(self) -> None:
        frame = self._flow_dirty_notice_pending
        if frame is not None:
            frame.encoded_text = self._encode_flow_dirty_notice(frame)
            if self._flow_dirty_notice_pending is frame:
                self._flow_dirty_notice_pending = None

    def _flow_install_receipt(self, resume: dict[str, Any]) -> _FlowInstallReceipt:
        if self._subscriptions is None:
            raise ValueError("Session is not subscribed on this connection")
        return (
            self._subscriptions.get_message_subscription_token(self.conn_id, resume["key"]),
            resume.get("snapshot_id"),
            resume.get("sync_revision"),
            resume.get("stream_generation"),
            resume.get("stream_seq"),
        )

    def snapshot_install_identity(self, resume: dict[str, Any]) -> tuple[str | None, int | None]:
        """Keep the frozen owner available for same-lease retries after bytes close."""
        receipt = self._flow_install_receipt(resume)
        if receipt[0] is None:
            raise ValueError("Session is not subscribed on this connection")
        installed = self._flow_installed.get(resume["key"])
        if installed is not None and installed[:-1] == receipt:
            return installed[-1]
        transfer = self._snapshot_transfer
        if transfer is None or not transfer.matches_install(resume):
            raise ValueError("Snapshot installation is not current")
        return transfer.identity

    def apply_flow_update(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._flow is None:
            raise ValueError("Consumption feedback was not negotiated")
        dirty_keys = params.get("dirty_keys", [])
        resumes = params.get("resume", [])
        for key in [*dirty_keys, *(item["key"] for item in resumes)]:
            if (
                self._subscriptions is None
                or self.conn_id not in self._subscriptions.get_message_subscribers(key)
            ):
                raise ValueError("Session is not subscribed on this connection")
        transfer = self._snapshot_transfer
        from opensquilla.gateway.session_streams import get_session_streams

        streams = get_session_streams()

        repeated: set[str] = set()
        for resume in resumes:
            key = resume["key"]
            installed = self._flow_installed.get(key)
            if (
                resume["stream_generation"] == streams.stream_generation
                and installed is not None
                and installed[:-1] == self._flow_install_receipt(resume)
            ):
                repeated.add(key)
                continue
            if transfer is None or not transfer.matches_install(resume):
                raise ValueError("Snapshot installation is not current")
        self._flow.validate_acknowledgement(params["delivery_epoch"], params["ack_delivery_id"])
        for delivery_id in params.get("staged_delivery_ids", []):
            self._flow.validate_stage(params["delivery_epoch"], delivery_id)
        self._flow.acknowledge(params["delivery_epoch"], params["ack_delivery_id"])
        for delivery_id in params.get("staged_delivery_ids", []):
            self._flow.stage(params["delivery_epoch"], delivery_id)
        self._prune_delivery_timers()
        for key in dirty_keys:
            if key in repeated:
                # Retrying a successful install may repeat its original
                # dirty declaration too; it must not recreate that barrier.
                continue
            self._flow.mark_dirty(key)
            if self._flow.global_dirty:
                self._subscriptions.set_message_flow_revision(self.conn_id, key, 0)
        for resume in resumes:
            key = resume["key"]
            if key in repeated:
                continue
            assert transfer is not None
            replay = streams.replay(key, resume["stream_seq"], resume["stream_generation"])
            tail_length = replay.current_stream_seq - resume["stream_seq"]
            expected = range(resume["stream_seq"] + 1, replay.current_stream_seq + 1)
            if (
                not replay.replay_complete
                or not 0 <= tail_length <= FLOW_WINDOW_FRAMES
                or [event.stream_seq for event in replay.events] != list(expected)
            ):
                transfer.close()
                self._flow.mark_dirty(key)
                self._queue_flow_dirty_notice()
                continue
            self._flow.dirty.pop(key, None)
            self._subscriptions.set_message_flow_revision(
                self.conn_id, key, self._flow.global_revision
            )
            for event in replay.events:
                projected = project_session_event_for_client(
                    event.event_name,
                    event.payload,
                    client_caps=self.client_caps,
                )
                if projected is None:
                    continue
                name, payload = projected
                self._enqueue_frame(
                    _OutboundFrame(
                        kind=f"event:{name}",
                        classification="control",
                        payload=payload,
                        event_name=name,
                        res_frame=None,
                        meta={"replayed": True},
                    )
                )
            self._flow_installed[key] = (
                *self._flow_install_receipt(resume),
                getattr(transfer, "identity", (None, None)),
            )
            self._flow_installed.move_to_end(key)
            while len(self._flow_installed) > FLOW_WINDOW_FRAMES:
                self._flow_installed.popitem(last=False)
            transfer.close()
        self._clear_covered_global_flow()
        return self._flow.status()

    def reserve_transport_bytes(self, size: int, *, kind: BudgetKind = None) -> bool:
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("transport reservation must be a nonnegative integer")
        kind = (kind or "bulk") if self._recovery_enabled else None
        headroom = sum(
            max(0, CONTROL_BUFFER_BYTES - self._transport_kinds.get(other, 0))
            for other in ("control", "recovery") if other != kind
        ) if kind is not None else 0
        if self._closing or self._transport_bytes + size > CONNECTION_BUFFER_BYTES - headroom:
            return False
        if not get_transport_budget().reserve(size, kind=kind):
            return False
        self._transport_bytes += size
        if kind is not None:
            self._transport_kinds[kind] = self._transport_kinds.get(kind, 0) + size
        return True

    def release_transport_bytes(self, size: int, *, kind: BudgetKind = None) -> None:
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= self._transport_bytes
        ):
            raise ValueError("transport release exceeds connection reservation")
        self._transport_bytes -= size
        kind = (kind or "bulk") if self._recovery_enabled else None
        if kind is not None:
            self._transport_kinds[kind] = self._transport_kinds.get(kind, 0) - size
        get_transport_budget().release(size, kind=kind)

    def add_transport_cleanup(self, callback: Callable[[], Any]) -> None:
        if self._closing:
            callback()
        else:
            self._transport_cleanup.append(callback)

    def transport_diagnostics(self) -> dict[str, int | bool]:
        """Aggregate counters only: never include keys, payloads or credentials."""
        flow = self._flow
        return {
            "queue_depth": self._outbox.qsize() if self._outbox is not None else 0,
            "queue_capacity": self._writer_queue_maxsize if self._queue_enabled else 0,
            "transport_reserved_bytes": self._transport_bytes,
            "global_transport_reserved_bytes": get_transport_budget().used,
            "ordinary_pending_requests": (
                self._ordinary_queue.qsize() if self._ordinary_queue is not None else 0
            ),
            "flow_enabled": flow is not None,
            "flow_unacked_frames": len(flow.deliveries) if flow is not None else 0,
            "flow_unacked_bytes": (
                sum(delivery.size for delivery in flow.deliveries.values()) if flow else 0
            ),
            "flow_ack_delivery_id": flow.ack_id if flow else 0,
            "flow_last_admitted_delivery_id": flow.next_id - 1 if flow else 0,
            "flow_dirty_sessions": len(flow.dirty) if flow else 0,
            "flow_global_dirty": bool(flow and flow.global_dirty),
            "control_queued_frames": self._flow_control_frames,
            "control_reserved_bytes": self._flow_control_bytes,
        }

    def _cleanup_transport(self) -> None:
        for timer in self._delivery_timers.values():
            timer.cancel()
        self._delivery_timers.clear()
        for operation in tuple(self._recovery_operations.values()):
            get_recovery_scheduler().cancel(operation)
        if self._outbox is not None:
            while not self._outbox.empty():
                item = self._outbox.get_nowait()
                if isinstance(item, _OutboundFrame):
                    self._release_outbound_budget(item)
        callbacks, self._transport_cleanup = self._transport_cleanup, []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                log.exception("gateway.ws_transport_cleanup_failed", conn_id=self.conn_id)

    def _enqueue_ordinary_request(
        self,
        request: Coroutine[Any, Any, None],
        size: int,
        *,
        provider_probe_lease: object | None = None,
        mutation_completion: asyncio.Future[None] | None = None,
    ) -> bool:
        if self._ordinary_stopped or self._closing:
            return False
        start_worker = self._ordinary_worker is None or self._ordinary_worker.done()
        if start_worker and len(_ORDINARY_WORKERS) >= _MAX_ORDINARY_WORKERS:
            return False
        if self._ordinary_queue is None:
            self._ordinary_queue = asyncio.Queue(maxsize=_MAX_ORDINARY_REQUESTS)
        if self._ordinary_queue.full() or not self.reserve_transport_bytes(size):
            return False
        self._ordinary_queue.put_nowait((request, size, provider_probe_lease, mutation_completion))
        if start_worker:
            self._ordinary_worker = asyncio.create_task(
                self._run_ordinary_requests(), name=f"ws-rpc-worker-{self.conn_id}"
            )
            _ORDINARY_WORKERS.add(self._ordinary_worker)
            self._ordinary_worker.add_done_callback(_ORDINARY_WORKERS.discard)
            self._ordinary_worker.add_done_callback(self._consume_task_result)
        return True

    def _try_recovery_request(
        self, dispatcher: RpcDispatcher, req_id: str, method: str, params: dict[str, Any],
        ctx: RpcContext, size: int,
    ) -> bool:
        from opensquilla.gateway.session_services import get_session_storage
        from opensquilla.gateway.snapshot_transfer import SnapshotTransferError
        from opensquilla.session.keys import canonicalize_session_key

        if req_id in self._recovery_operations:
            return False
        raw_key = params.get("key", params.get("sessionKey", "agent:main:webchat"))
        if method == "transport.flow.update":
            raw_key = params["resume"][0]["key"]
        key = canonicalize_session_key(raw_key)
        runtime = get_session_storage(ctx.session_manager) or ctx.session_manager or get_registry()
        self._recovery_runtime = runtime
        scheduler = get_recovery_scheduler()
        predecessors: list[asyncio.Future[Any]] = []
        tail = scheduler.mutation_tail(runtime, key)
        if tail is not None:
            predecessors.append(tail)
        intent = None
        created = False
        transfer_registry = None
        transfer_created = False
        if self._subscriptions is not None:
            intent = self._subscriptions.get_message_intent(self.conn_id, key)
            if method == "sessions.messages.subscribe":
                intent, created = self._subscriptions.admit_message_subscription(self.conn_id, key)
            elif intent is not None and not intent.ready.done():
                predecessors.append(intent.ready)
        admitted_at = time.monotonic()
        read_deadline = admitted_at + READ_BUDGET_SECONDS
        operation = RecoveryOperation(
            req_id, self.conn_id, method, key, runtime,
            admitted_at + (15.0 if method == "sessions.messages.snapshot.read" else
                           READ_BUDGET_SECONDS),
            predecessors=tuple(predecessors),
            subscription_token=intent.token if intent else None,
            subscription_created=created,
        )

        def current() -> bool:
            return not self._closing and get_registry().get(self.conn_id) is self and (
                intent is None or (
                    not intent.closed
                    and self._subscriptions.get_message_intent(self.conn_id, key) is intent
                )
            )

        operation.is_current = current
        if self._recovery_enabled and method in {
            "sessions.messages.snapshot.read", "sessions.messages.resume",
        }:
            try:
                registry = self.snapshot_registry()
                if params.get("snapshot_id") is not None:
                    operation.transfer = registry.get(
                        key, params["sync_revision"], params["snapshot_id"],
                    )
                    if operation.transfer is None and not (
                        method == "sessions.messages.resume"
                        and self.installed_snapshot_proof(params)
                    ):
                        raise SnapshotTransferError("SNAPSHOT_EXPIRED")
                else:
                    existing_transfer = registry.get(key, params["sync_revision"])
                    operation.transfer = registry.admit(
                        key, params["sync_revision"], operation.subscription_token,
                        is_current=current,
                    )
                    transfer_registry = registry
                    transfer_created = existing_transfer is None
            except SnapshotTransferError as exc:
                self._enqueue_frame(_OutboundFrame(
                    "res", "control", None, None,
                    make_error_res(req_id, exc.code, "Snapshot is no longer available",
                                   retryable=True, accepted=False),
                    is_control=True,
                ))
                return True

        def release_admitted_transfer() -> None:
            if transfer_created and transfer_registry is not None:
                transfer_registry.release(key, params["sync_revision"])

        if not self.reserve_transport_bytes(size):
            release_admitted_transfer()
            if created:
                self._subscriptions.unsubscribe_messages(
                    self.conn_id, key, expected_token=operation.subscription_token,
                )
            return False
        self._recovery_operations[req_id] = operation

        def finish() -> None:
            if self._recovery_operations.get(req_id) is operation:
                del self._recovery_operations[req_id]
            self.release_transport_bytes(size)
            if created and intent is not None and not intent.ready.done():
                self._subscriptions.unsubscribe_messages(
                    self.conn_id, key, expected_token=intent.token,
                )
            if self._flow is not None and operation.transfer is not None:
                for delivery_id, delivery in tuple(self._flow.deliveries.items()):
                    if delivery.owner is operation.transfer and delivery.publication == "reserved":
                        self.cancel_snapshot_delivery({
                            "delivery_epoch": self._flow.epoch, "delivery_id": delivery_id,
                        })

        def expire() -> None:
            if not self._closing:
                self._enqueue_frame(_OutboundFrame(
                    "res", "control", None, None,
                    make_error_res(req_id, "STORAGE_BUSY", "Recovery read deadline exceeded",
                                   retryable=True, accepted=False),
                    is_control=True,
                ))

        async def run() -> None:
            from opensquilla.session.recovery_reads import ReadCancelToken, recovery_read_scope

            token = ReadCancelToken()
            operation.cancel_callbacks.append(token.cancel)
            with recovery_read_scope(
                key, deadline=read_deadline, cancel_token=token,
                workload="identity" if method in {
                    "sessions.messages.resume", "transport.flow.update",
                } else "bulk",
            ) as budget:
                try:
                    await _dispatch_request(self, dispatcher, req_id, method, params, ctx)
                finally:
                    await budget.drain()

        if not scheduler.submit(operation, run, finish=finish, expire=expire):
            release_admitted_transfer()
            finish()
            return False
        return True

    def retire_snapshot(self, key: str, revision: str, snapshot_id: str | None = None) -> None:
        proof = self._resume_proofs.get(key)
        if proof is not None and proof["sync_revision"] == revision and (
            snapshot_id is None or proof["snapshot_id"] == snapshot_id
        ):
            self._resume_proofs.pop(key, None)
        registry = self._snapshot_registry
        if registry is None:
            return
        transfer = registry.get(key, revision, snapshot_id)
        if transfer is not None:
            registry.release(key, revision, snapshot_id)
            for operation in tuple(self._recovery_operations.values()):
                if operation.transfer is transfer:
                    get_recovery_scheduler().cancel(operation)

    def install_snapshot(
        self, params: dict[str, Any], transfer: Any, dirty_revision: Any,
    ) -> dict[str, Any]:
        """Commit a prepared installation and its replay without yielding."""
        from opensquilla.gateway.session_streams import get_session_streams
        from opensquilla.gateway.snapshot_transfer import SnapshotTransferError

        flow = self._flow
        key = params["key"]
        if (
            flow is None or self._subscriptions is None or self._closing
            or transfer.closed or not transfer.matches_install(params)
            or transfer.lease_token is None
            or transfer.lease_token != self._subscriptions.get_message_subscription_token(
                self.conn_id, key,
            )
            or flow.dirty_revision(key) != dirty_revision
        ):
            raise SnapshotTransferError("SNAPSHOT_STALE")
        replay = get_session_streams().replay(
            key, params["stream_seq"], params["stream_generation"],
        )
        if (
            not replay.replay_complete
            or not 0 <= replay.current_stream_seq - params["stream_seq"] <= FLOW_WINDOW_FRAMES
            or [event.stream_seq for event in replay.events]
            != list(range(params["stream_seq"] + 1, replay.current_stream_seq + 1))
        ):
            raise SnapshotTransferError("SNAPSHOT_STALE")
        frames: list[_OutboundFrame] = []
        total_bytes = 0
        visible_tail_seq = params["stream_seq"]
        for event in replay.events:
            projected = project_session_event_for_client(
                event.event_name, event.payload, client_caps=self.client_caps,
            )
            if projected is None:
                continue
            name, payload = projected
            # Preflight both ledger quota and real shared bytes before any
            # installed state changes. No producer runs between this and commit.
            encoded = make_event(name, encode_payload_for_protocol(payload, protocol=self.protocol),
                                 meta={"replayed": True, "flow": {
                                     "delivery_epoch": flow.epoch,
                                     "delivery_id": flow.next_id + len(frames),
                                 }}).model_dump_json(exclude={"seq"})
            total_bytes += len(encoded.encode("utf-8")) + 32
            frames.append(_OutboundFrame(
                f"event:{name}", "control", payload, name, None, meta={"replayed": True},
            ))
            visible_tail_seq = event.stream_seq
        proof = {
            **params, "session_id": transfer.identity[0], "session_epoch": transfer.identity[1],
            "replay_to_seq": visible_tail_seq,
        }
        operation = CURRENT_RECOVERY_OPERATION.get()
        proof_bytes = len(make_ok_res(
            operation.request_id if operation is not None else "", proof,
        ).model_dump_json().encode("utf-8"))
        self._freeze_pending_dirty_notice()
        ordinary = [delivery for delivery in flow.deliveries.values() if not delivery.recovery]
        if (
            self._outbox is None
            or self._outbox.qsize() + len(frames) + 1 > self._outbox.maxsize
            or len(ordinary) + len(frames) > FLOW_WINDOW_FRAMES
            or sum(item.size for item in ordinary) + total_bytes > FLOW_WINDOW_BYTES
            or self._flow_control_frames >= CONTROL_BUFFER_FRAMES
            or self._flow_control_bytes + proof_bytes > CONTROL_BUFFER_BYTES
            or not self.reserve_transport_bytes(total_bytes)
        ):
            raise SnapshotTransferError("SNAPSHOT_BUSY")
        self.release_transport_bytes(total_bytes)
        if not self.reserve_transport_bytes(proof_bytes, kind="control"):
            raise SnapshotTransferError("SNAPSHOT_BUSY")
        self.release_transport_bytes(proof_bytes, kind="control")
        flow.dirty.pop(key, None)
        self._subscriptions.set_message_flow_revision(self.conn_id, key, flow.global_revision)
        for frame in frames:
            self._enqueue_frame(frame)
        self._clear_covered_global_flow()
        self._flow_installed[key] = (*self._flow_install_receipt(params), transfer.identity)
        self._flow_installed.move_to_end(key)
        while len(self._flow_installed) > FLOW_WINDOW_FRAMES:
            self._flow_installed.popitem(last=False)
        self._resume_proofs[key] = proof
        self._resume_proofs.move_to_end(key)
        while len(self._resume_proofs) > FLOW_WINDOW_FRAMES:
            self._resume_proofs.popitem(last=False)
        transfer.close()
        return proof

    def installed_snapshot_proof(self, params: dict[str, Any]) -> dict[str, Any] | None:
        proof = self._resume_proofs.get(params["key"])
        installed = self._flow_installed.get(params["key"])
        if proof is not None and installed is not None and (
            installed[:-1] == self._flow_install_receipt(params)
            and all(proof.get(name) == value for name, value in params.items())
        ):
            return dict(proof)
        return None

    async def _run_ordinary_requests(self) -> None:
        assert self._ordinary_queue is not None
        # No await on an empty queue: the reader creates the next worker only
        # after admitting another request, so an idle connection has no worker.
        while not self._ordinary_stopped and not self._ordinary_queue.empty():
            request, size, provider_probe_lease, mutation_completion = (
                self._ordinary_queue.get_nowait()
            )
            try:
                await request
            finally:
                try:
                    self.release_transport_bytes(size)
                finally:
                    if provider_probe_lease is not None:
                        _release_provider_probe_lease(provider_probe_lease)
                    if mutation_completion is not None and not mutation_completion.done():
                        mutation_completion.set_result(None)

    async def _stop_ordinary_requests(self) -> None:
        self._ordinary_stopped = True
        if self._ordinary_queue is not None:
            while not self._ordinary_queue.empty():
                request, size, provider_probe_lease, mutation_completion = (
                    self._ordinary_queue.get_nowait()
                )
                try:
                    request.close()
                    self.release_transport_bytes(size)
                finally:
                    if provider_probe_lease is not None:
                        _release_provider_probe_lease(provider_probe_lease)
                    if mutation_completion is not None and not mutation_completion.done():
                        mutation_completion.set_result(None)
        worker = self._ordinary_worker
        if worker is None:
            return
        if not worker.done():
            await asyncio.wait({worker}, timeout=_ORDINARY_DRAIN_SECONDS)
        if worker.done():
            self._consume_task_result(worker)
        else:
            _DRAINING_ORDINARY_WORKERS.add(worker)
            worker.add_done_callback(_DRAINING_ORDINARY_WORKERS.discard)
            worker.add_done_callback(self._consume_task_result)

    @property
    def role(self) -> str:
        return self.principal.role

    @property
    def scopes(self) -> list[str]:
        return list(self.principal.scopes)

    @property
    def authenticated(self) -> bool:
        return self.principal.authenticated

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except BaseException:
            pass

    def _try_start_detached_read(
        self,
        awaitable: Coroutine[Any, Any, None],
        *,
        method: str,
    ) -> bool:
        # Session switches and bounded retries can briefly overlap history
        # reads. Keep that overlap bounded without ever falling back to the
        # serial receive loop, which would recreate head-of-line blocking.
        if len(self._detached_read_tasks) >= _MAX_DETACHED_READS_PER_CONNECTION:
            return False
        task = asyncio.create_task(
            awaitable,
            name=f"ws-read-{method}-{self.conn_id}",
        )
        self._detached_read_tasks.add(task)
        task.add_done_callback(self._handle_detached_read_result)
        return True

    def _handle_detached_read_result(self, task: asyncio.Task[None]) -> None:
        self._detached_read_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except BaseException:
            return
        if error is None:
            return
        log.warning(
            "gateway.ws_detached_read_failed",
            conn_id=self.conn_id,
            task_name=task.get_name(),
            error=str(error),
        )
        # A request failure cannot retire unrelated work on this connection.
        # _dispatch_request converts handler failures into a request-local error.

    async def _stop_detached_reads(self) -> None:
        self._closing = True
        tasks = tuple(self._detached_read_tasks)
        self._detached_read_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(
                tasks,
                timeout=_DETACHED_READ_STOP_TIMEOUT_SECONDS,
            )
            if pending:
                log.warning(
                    "gateway.ws_stop_detached_reads_timeout",
                    conn_id=self.conn_id,
                    pending_count=len(pending),
                )

    async def _send_direct_text(self, text: str) -> None:
        """Bound legacy direct sends so a wedged socket cannot stall an RPC."""

        if self._closing or self.ws.client_state != WebSocketState.CONNECTED:
            return
        send_task = asyncio.create_task(
            self.ws.send_text(text),
            name=f"ws-direct-send-{self.conn_id}",
        )
        try:
            done, _ = await asyncio.wait(
                {send_task},
                timeout=_DIRECT_SEND_TIMEOUT_SECONDS,
            )
        except BaseException:
            send_task.cancel()
            send_task.add_done_callback(self._consume_task_result)
            self._closing = True
            raise
        if send_task in done:
            try:
                await send_task
            except BaseException:
                self._closing = True
                raise
            return

        send_task.cancel()
        send_task.add_done_callback(self._consume_task_result)
        self._closing = True
        log.warning(
            "gateway.ws_direct_send_timeout",
            conn_id=self.conn_id,
            timeout_seconds=_DIRECT_SEND_TIMEOUT_SECONDS,
        )

        close_task = asyncio.create_task(
            self.ws.close(code=1011, reason="direct_send_timeout"),
            name=f"ws-direct-close-{self.conn_id}",
        )
        done, _ = await asyncio.wait(
            {close_task},
            timeout=_DIRECT_CLOSE_TIMEOUT_SECONDS,
        )
        if close_task in done:
            try:
                await close_task
            except Exception:
                pass
        else:
            close_task.cancel()
            close_task.add_done_callback(self._consume_task_result)
        raise TimeoutError("WebSocket direct send timed out")

    # ------------------------------------------------------------------
    # Public send entry points
    # ------------------------------------------------------------------

    async def send_event(
        self,
        event: str,
        payload: Any = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if self._closing:
            return
        projected = project_session_event_for_client(
            event,
            payload,
            client_caps=self.client_caps,
        )
        if projected is None:
            return
        event, payload = projected
        # Atomic check + enqueue. The check and ``put_nowait`` are part of
        # one synchronous flow with no ``await`` between them, so
        # ``_force_close`` cannot flip ``_closing`` mid-flight (asyncio is
        # single-threaded; only awaits yield).
        if self._queue_enabled and self._outbox is not None and not self._closing:
            classification = "lossy" if event in _LOSSY_EVENTS else "control"
            frame = _OutboundFrame(
                kind=f"event:{event}",
                classification=classification,
                payload=payload,
                event_name=event,
                res_frame=None,
                meta=meta,
            )
            self._enqueue_frame(frame)
            # A producer can emit hundreds of tool/text deltas through an
            # await chain whose queue fast path never actually suspends. Give
            # the healthy writer a chance to drain at half capacity before a
            # cooperative burst mistakes event-loop starvation for a slow
            # consumer and force-closes the connection at the hard limit.
            if not self._closing and self._outbox.qsize() >= max(
                1, self._writer_queue_maxsize // 2
            ):
                await asyncio.sleep(0)
            return
        # Legacy direct-send path (pre-auth, kill-switch off, or post-stop).
        async with self._send_lock:
            if not self._closing and self.ws.client_state == WebSocketState.CONNECTED:
                wire = make_event(
                    event,
                    encode_payload_for_protocol(payload, protocol=self.protocol),
                    seq=self.next_seq(),
                    meta=meta,
                )
                await self._send_direct_text(wire.model_dump_json())

    async def send_res(self, frame: ResFrame, *, transport_control: bool = False) -> None:
        if self._closing:
            return
        # RPC responses are always CONTROL: they carry state-bearing payloads
        # and a slow-client overflow must close the connection rather than
        # silently dropping the response.
        if self._queue_enabled and self._outbox is not None and not self._closing:
            outbound = _OutboundFrame(
                kind="res",
                classification="control",
                payload=None,
                event_name=None,
                res_frame=frame,
                # Non-droppable response ordering is independent of memory
                # class: history pages may exceed the small control reserve.
                is_control=self._recovery_enabled and (transport_control or not frame.ok),
            )
            self._enqueue_frame(outbound)
            return
        async with self._send_lock:
            if not self._closing and self.ws.client_state == WebSocketState.CONNECTED:
                encoded = frame.model_copy(
                    update={
                        "payload": encode_payload_for_protocol(
                            frame.payload,
                            protocol=self.protocol,
                        )
                    }
                )
                await self._send_direct_text(encoded.model_dump_json())

    async def send_raw_text(self, text: str) -> None:
        """Send a protocol-level raw frame through the connection writer."""

        if self._closing:
            return
        if self._queue_enabled and self._outbox is not None:
            self._enqueue_frame(
                _OutboundFrame(
                    kind="raw",
                    classification="control",
                    payload=None,
                    event_name=None,
                    res_frame=None,
                    raw_text=text,
                )
            )
            return
        async with self._send_lock:
            if not self._closing and self.ws.client_state == WebSocketState.CONNECTED:
                await self._send_direct_text(text)

    async def close(self, code: int = WS_CLOSE_SERVICE_RESTART, reason: str = "") -> None:
        self._closing = True
        if len(_SOCKET_CLOSE_TASKS) >= _MAX_WRITER_TASKS:
            return
        task = asyncio.create_task(
            self.ws.close(code=code, reason=reason) if reason else self.ws.close(code=code),
            name="gateway-socket-close",
        )
        _SOCKET_CLOSE_TASKS.add(task)
        task.add_done_callback(_SOCKET_CLOSE_TASKS.discard)
        task.add_done_callback(self._consume_task_result)
        done, _ = await asyncio.wait({task}, timeout=_DIRECT_CLOSE_TIMEOUT_SECONDS)
        if not done:
            task.cancel()

    def _track_detached_request(
        self,
        task: asyncio.Task[None],
        *,
        request_id: str | None = None,
        provider_probe_lease: object | None = None,
    ) -> None:
        self._detached_request_tasks.add(task)
        if request_id is not None:
            self._cancellable_request_tasks[request_id] = task

        def finished(completed: asyncio.Task[None]) -> None:
            self._detached_request_tasks.discard(completed)
            if provider_probe_lease is not None:
                _release_provider_probe_lease(provider_probe_lease)
            self._cancelled_request_tasks.discard(completed)
            if (
                request_id is not None
                and self._cancellable_request_tasks.get(request_id) is completed
            ):
                self._cancellable_request_tasks.pop(request_id, None)
            if completed.cancelled():
                return
            try:
                error = completed.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                log.error(
                    "gateway.ws_detached_request_failed",
                    conn_id=self.conn_id,
                    error=str(error),
                )

        task.add_done_callback(finished)

    def _cancel_detached_request(self, request_id: str) -> None:
        """Cancel a cancellable request owned by this connection, if still active."""

        task = self._cancellable_request_tasks.get(request_id)
        if task is None or task.done():
            return
        self._cancelled_request_tasks.add(task)
        task.cancel()

    def _detached_response_allowed(
        self,
        task: asyncio.Task[None] | None,
    ) -> bool:
        return (
            self._accept_detached_responses
            and task is not None
            and task not in self._cancelled_request_tasks
        )

    async def _stop_detached_requests(self) -> None:
        self._accept_detached_responses = False
        tasks = tuple(self._detached_request_tasks)
        self._detached_request_tasks.clear()
        self._cancellable_request_tasks.clear()
        self._cancelled_request_tasks.update(tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(
                tasks,
                timeout=_DETACHED_REQUEST_DRAIN_SECONDS,
            )
            if pending:
                log.warning(
                    "gateway.ws_detached_request_drain_timeout",
                    conn_id=self.conn_id,
                    pending=len(pending),
                )

    # ------------------------------------------------------------------
    # Writer task lifecycle
    # ------------------------------------------------------------------

    def _start_writer(self, *, maxsize: int, enabled: bool) -> None:
        """Idempotently boot the per-connection writer task.

        Called from ``handle_ws_connection`` immediately after
        ``registry.register(conn)``. Pre-auth sends do NOT go through the
        queue because the writer task does not exist yet — see Step 4 of
        the plan and the comment block at the registration call site.
        """
        if self._writer_task is not None:
            return
        self._queue_enabled = bool(enabled)
        self._writer_queue_maxsize = int(maxsize)
        if not self._queue_enabled:
            return
        if len(_WRITER_TASKS) >= _MAX_WRITER_TASKS:
            self._closing = True
            task = asyncio.create_task(self.close(code=1013, reason="writer_capacity"))
            task.add_done_callback(self._consume_task_result)
            return
        self._outbox = asyncio.Queue(maxsize=self._writer_queue_maxsize)
        self._writer_task = asyncio.create_task(
            self._writer_loop(), name=f"ws-writer-{self.conn_id}"
        )
        _WRITER_TASKS.add(self._writer_task)
        self._writer_task.add_done_callback(_WRITER_TASKS.discard)
        self._writer_task.add_done_callback(self._consume_task_result)
        log.debug("gateway.ws_writer_started", conn_id=self.conn_id)

    async def _stop_writer(self) -> None:
        """Idempotent writer shutdown for the disconnect path.

        Unlike ``_force_close`` this does NOT call ``ws.close()`` — clean
        disconnects are already signaled by ``WebSocketDisconnect`` and the
        socket is already torn down by the time we hit the ``finally`` of
        ``handle_ws_connection``. Calling ws.close() here would race with
        starlette's own teardown.
        """
        self._closing = True
        task = self._writer_task
        if task is None:
            return
        self._writer_task = None
        # Best-effort wakeup for a writer blocked in ``outbox.get()``.
        if self._outbox is not None:
            try:
                self._outbox.put_nowait(_SENTINEL_STOP)
            except asyncio.QueueFull:
                pass
        if not task.done():
            task.cancel()
            _DRAINING_WRITER_TASKS.add(task)
            task.add_done_callback(_DRAINING_WRITER_TASKS.discard)
            done, _ = await asyncio.wait({task}, timeout=_WRITER_STOP_SECONDS)
            if not done:
                log.warning(
                    "gateway.ws_stop_writer_timeout",
                    conn_id=self.conn_id,
                )
        log.debug("gateway.ws_writer_stopped", conn_id=self.conn_id)

    async def _force_close(self, *, reason: str, code: int = 1011) -> None:
        """Forcefully tear down the connection due to writer backpressure.

        Idempotent. The ``_writer_task is None`` marker doubles as the
        "already-completed force_close" sentinel: the first invocation
        claims the task atomically, cancels it with a bounded timeout,
        then closes the socket. Concurrent invocations no-op.
        """
        self._closing = True
        task = self._writer_task
        if task is None:
            # Either there was never a writer (legacy mode) or another
            # force_close already ran. Either way: nothing to do.
            return
        # Atomically claim ownership so concurrent calls see _writer_task=None.
        self._writer_task = None
        if not task.done():
            task.cancel()
            _DRAINING_WRITER_TASKS.add(task)
            task.add_done_callback(_DRAINING_WRITER_TASKS.discard)
            done, _ = await asyncio.wait({task}, timeout=_WRITER_STOP_SECONDS)
            if not done:
                log.warning(
                    "gateway.ws_writer_force_close_timeout",
                    conn_id=self.conn_id,
                    reason=reason,
                )
        await self.close(code=code, reason=reason)

    # ------------------------------------------------------------------
    # Writer loop and enqueue helper
    # ------------------------------------------------------------------

    def _release_outbound_budget(self, frame: _OutboundFrame) -> None:
        if frame.budget_bytes:
            self.release_transport_bytes(frame.budget_bytes, kind=frame.budget_kind)
            if frame.is_control:
                self._flow_control_bytes -= frame.budget_bytes
                self._flow_control_frames -= 1
            frame.budget_bytes = 0

    def _encode_flow_dirty_notice(self, frame: _OutboundFrame) -> str:
        """Refresh one notice without allowing long keys to consume the control lane.

        The full dirty set remains authoritative on the connection. A compact
        global invalidation is sufficient when spelling it out would exceed
        this notice's allowance; normal flow replies still expose dirty keys.
        """
        assert self._flow is not None
        payload = self._flow.dirty_notice()
        # Bound even the temporary encoded result. Per-key encoding is at
        # most 4096 codepoints, and iteration stops before a giant list joins.
        estimated_size = 1024
        for key in payload["dirty_keys"]:
            estimated_size += len(json.dumps(key, ensure_ascii=False).encode("utf-8")) + 1
            if estimated_size > CONTROL_BUFFER_BYTES // 2:
                break
        fallback = {
            "delivery_epoch": self._flow.epoch,
            "dirty_keys": [],
            "global_dirty": True,
        }
        if estimated_size > CONTROL_BUFFER_BYTES // 2:
            payload = fallback
        text = make_event("transport.flow.dirty", payload).model_dump_json(exclude={"seq"})
        required = len(text.encode("utf-8")) + 32
        extra = max(0, required - frame.budget_bytes)
        if extra:
            # Leave room for bounded pong/probe traffic even with a large
            # coalesced invalidation. Never close a healthy peer just because
            # its session names need many JSON escape bytes.
            if (
                self._flow_control_bytes + extra <= CONTROL_BUFFER_BYTES - 64 * 1024
                and self.reserve_transport_bytes(extra, kind="control")
            ):
                frame.budget_bytes += extra
                self._flow_control_bytes += extra
            else:
                text = make_event("transport.flow.dirty", fallback).model_dump_json(exclude={"seq"})
        return text

    def _prepare_flow_frame(self, frame: _OutboundFrame) -> bool:
        assert self._flow is not None
        if frame.encoded_text is not None:
            return True
        if frame.event_name is not None:
            is_stream = frame.event_name.startswith("session.event.")
            key = _payload_field(frame.payload, "session_key") or _payload_field(
                frame.payload, "key"
            )
            if is_stream and (key in self._flow.dirty or self._flow_key_needs_global_recovery(key)):
                # The existing barrier already owns these intentionally
                # suppressed deltas. Resume proves raw replay coverage; a new
                # token here would starve installation during a live stream.
                prior = self._flow.dirty.get(key)
                sequence = _payload_field(frame.payload, "stream_seq")
                generation = _payload_field(frame.payload, "stream_generation")
                if (prior is not None and isinstance(sequence, int)
                        and not isinstance(sequence, bool)):
                    self._flow.dirty[key] = (
                        generation if isinstance(generation, str) else prior[0],
                        max(prior[1], sequence),
                    )
                return False
            meta = dict(frame.meta or {})
            if is_stream:
                meta["flow"] = {
                    "delivery_epoch": self._flow.epoch,
                    "delivery_id": self._flow.next_id,
                }
            encoded = make_event(
                frame.event_name,
                encode_payload_for_protocol(frame.payload, protocol=self.protocol),
                meta=meta or None,
            ).model_dump_json(exclude={"seq"})
            encoded_size = len(encoded.encode("utf-8"))
            size = encoded_size + 32  # bounded writer-assigned seq field
            # This synchronous FIFO admission cannot be overtaken by another
            # producer. Responses do not mint seq; only queued events count.
            assert self._outbox is not None
            pending_events = sum(
                isinstance(queued, _OutboundFrame) and queued.event_name is not None
                for queued in self._outbox._queue  # type: ignore[attr-defined]
            )
            wire_size = encoded_size + len(f',"seq":{self._seq + pending_events + 1}')
            if frame.event_name == "transport.flow.dirty" and frame.delivery_id is None:
                # The writer refreshes this coalesced notice with every dirty
                # key discovered while it was queued. Reserve an initial
                # allowance; the writer bounds and accounts for any growth.
                size = max(size, 256 * 1024)
            if is_stream:
                delivery_id = self._flow.admit(size, wire_size=wire_size)
                if delivery_id is None:
                    self._mark_flow_dirty(frame.payload)
                    return False
                frame.delivery_id = delivery_id
                frame.encoded_text = encoded
                return True
        elif frame.res_frame is not None:
            encoded = frame.res_frame.model_copy(
                update={
                    "payload": encode_payload_for_protocol(
                        frame.res_frame.payload, protocol=self.protocol
                    ),
                }
            ).model_dump_json()
            size = len(encoded.encode("utf-8"))
            wire_size = size
            if wire_size > MAX_PAYLOAD_BYTES:
                raise ValueError("Outbound frame exceeds the wire limit")
            receipt = _payload_field(frame.res_frame.payload, "delivery")
            if isinstance(receipt, dict) and receipt.get("delivery_epoch") == self._flow.epoch:
                delivery_id = receipt.get("delivery_id")
                if not isinstance(delivery_id, int) or isinstance(delivery_id, bool):
                    raise ValueError("Snapshot delivery reservation is not current")
                delivery = self._flow.deliveries.get(delivery_id)
                if delivery is None or not delivery.recovery:
                    raise ValueError("Snapshot delivery reservation is not current")
                extra = max(0, size - delivery.size)
                if extra and not self.reserve_transport_bytes(extra, kind="recovery"):
                    raise ValueError("Snapshot response exceeds the transport budget")
                delivery.size += extra
                if self._recovery_enabled and not self._flow.claim(delivery_id, "original"):
                    return False
                frame.delivery_id = delivery_id
                frame.encoded_text = encoded
                return True
        elif frame.raw_text is not None:
            encoded = frame.raw_text
            size = len(encoded.encode("utf-8"))
            wire_size = size
            frame.is_control = True
        else:
            return False
        if wire_size > MAX_PAYLOAD_BYTES:
            raise ValueError("Outbound frame exceeds the wire limit")
        if frame.is_control and (
            self._flow_control_frames >= CONTROL_BUFFER_FRAMES
            or self._flow_control_bytes + size > CONTROL_BUFFER_BYTES
        ):
            raise ValueError("Control buffer is full")
        frame.budget_kind = "control" if frame.is_control else None
        if not self.reserve_transport_bytes(size, kind=frame.budget_kind):
            raise ValueError("Connection transport budget is full")
        frame.budget_bytes = size
        if frame.is_control:
            self._flow_control_frames += 1
            self._flow_control_bytes += size
        frame.encoded_text = encoded
        return True

    async def _writer_loop(self) -> None:
        """Drain ``_outbox`` and serialize frames onto the wire.

        WS-frame ``seq`` is minted here, at dequeue. This guarantees a
        contiguous monotonic ``seq`` even when producers' lossy frames are
        dropped by ``_enqueue_frame`` — drops never consume a seq.
        """
        assert self._outbox is not None
        try:
            while True:
                item = await self._outbox.get()
                if item is _SENTINEL_STOP:
                    return
                if not isinstance(item, _OutboundFrame):
                    continue
                if self._closing or self.ws.client_state != WebSocketState.CONNECTED:
                    # This frame is no longer in the queue drained during
                    # teardown. Its non-ledger reservation belongs to us.
                    self._release_outbound_budget(item)
                    return
                try:
                    if item.encoded_text is not None:
                        text = item.encoded_text
                        if (
                            self._flow_dirty_notice_pending is item
                            and self._flow is not None
                        ):
                            self._freeze_pending_dirty_notice()
                            assert item.encoded_text is not None
                            text = item.encoded_text
                        if item.event_name is not None:
                            text = text[:-1] + f',"seq":{self.next_seq()}' + "}"
                    elif item.event_name is not None:
                        text = make_event(
                            item.event_name,
                            encode_payload_for_protocol(
                                item.payload,
                                protocol=self.protocol,
                            ),
                            seq=self.next_seq(),
                            meta=item.meta,
                        ).model_dump_json()
                    elif item.res_frame is not None:
                        text = item.res_frame.model_copy(
                            update={
                                "payload": encode_payload_for_protocol(
                                    item.res_frame.payload,
                                    protocol=self.protocol,
                                )
                            }
                        ).model_dump_json()
                    elif item.raw_text is not None:
                        text = item.raw_text
                    else:
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A frame that cannot be serialized (e.g. a lone surrogate
                    # in a payload) must not silently kill this task: the
                    # socket would stay open, requests would keep executing,
                    # and no response would ever leave. Close instead so the
                    # reader loop tears the connection down normally.
                    log.warning(
                        "gateway.ws_frame_serialize_failed",
                        conn_id=self.conn_id,
                        exc_info=True,
                    )
                    self._closing = True
                    try:
                        await self.ws.close(code=1011)
                    except Exception:  # noqa: BLE001
                        pass
                    return
                try:
                    if self._flow is not None and item.delivery_id is not None:
                        self._flow.mark_sending(item.delivery_id)
                    async with asyncio.timeout(60.0):
                        await self.ws.send_text(text)
                    if self._flow is not None and item.delivery_id is not None:
                        self._flow.mark_sent(item.delivery_id)
                except WebSocketDisconnect:
                    self._closing = True
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.debug(
                        "gateway.ws_writer_send_failed",
                        conn_id=self.conn_id,
                        exc_info=True,
                    )
                    self._closing = True
                    try:
                        await self.ws.close(code=1011, reason="writer_send_failed")
                    except Exception:  # noqa: BLE001
                        pass
                    return
                finally:
                    if self._flow is not None and item.delivery_id is not None:
                        self._flow.mark_send_finished(item.delivery_id)
                    self._release_outbound_budget(item)
                    # The next queue wait may last for the connection's entire
                    # lifetime. Do not retain an uncharged completed frame.
                    del item, text
                if self._closing:
                    return
        except asyncio.CancelledError:
            raise

    def _enqueue_frame(self, frame: _OutboundFrame) -> None:
        """Synchronous enqueue with classification-aware overflow.

        Caller has already verified ``_queue_enabled`` and ``not _closing``
        and that ``_outbox is not None``. This method MUST NOT ``await`` —
        a yield point here would let ``_force_close`` flip ``_closing``
        between the guard check in ``send_event`` and the enqueue mutation.
        """
        if self._outbox is None:
            return
        if self._flow is not None:
            if frame.classification == "lossy" and self._outbox.full():
                return
            try:
                if not self._prepare_flow_frame(frame):
                    return
            except Exception:
                log.warning(
                    "gateway.ws_flow_encode_or_budget_failed", conn_id=self.conn_id, exc_info=True
                )
                if frame.event_name and frame.event_name.startswith("session.event."):
                    self._mark_flow_dirty(frame.payload)
                    return
                # A non-replayable response/control cannot be silently lost.
                self._closing = True
                task = asyncio.create_task(
                    self._force_close(reason="transport_resource_limit", code=1013)
                )
                task.add_done_callback(self._consume_task_result)
                return
        try:
            self._outbox.put_nowait(frame)
            return
        except asyncio.QueueFull:
            pass

        if self._flow is not None:
            self._release_outbound_budget(frame)

        if frame.classification == "lossy":
            evicted = self._evict_oldest_same_kind(frame.kind)
            if evicted:
                try:
                    self._outbox.put_nowait(frame)
                    log.warning(
                        "gateway.ws_writer_drop",
                        conn_id=self.conn_id,
                        event_name=frame.event_name,
                        session_key=_payload_field(frame.payload, "session_key"),
                        stream_seq=_payload_field(frame.payload, "stream_seq"),
                        queue_depth=self._outbox.qsize(),
                        eviction=True,
                    )
                    return
                except asyncio.QueueFull:
                    pass
            # No same-kind candidate or impossibly rare race: drop the new
            # incoming frame to keep the close path moving.
            log.warning(
                "gateway.ws_writer_drop",
                conn_id=self.conn_id,
                event_name=frame.event_name,
                session_key=_payload_field(frame.payload, "session_key"),
                stream_seq=_payload_field(frame.payload, "stream_seq"),
                queue_depth=self._outbox.qsize(),
                eviction=False,
            )
            return

        # CONTROL overflow: cannot drop, cannot block. Schedule force-close.
        # Same-kind eviction policy note: under R-B the lossy set is {tick},
        # which has no session_key, so eviction is keyed on event_name only.
        # If the lossy set is later expanded to session-bearing events, the
        # eviction key MUST become (event_name, session_key) to prevent one
        # session's overflow from evicting another session's queued frame.
        # Keep this invariant if more lossy event kinds are added later.
        self._closing = True
        log.error(
            "gateway.ws_writer_overflow_close",
            conn_id=self.conn_id,
            event_name=frame.event_name,
            session_key=_payload_field(frame.payload, "session_key"),
            stream_seq=_payload_field(frame.payload, "stream_seq"),
            queue_depth=self._outbox.qsize(),
        )
        asyncio.create_task(
            self._force_close(reason="writer_backpressure", code=1011),
            name=f"ws-force-close-{self.conn_id}",
        )

    def _evict_oldest_same_kind(self, kind: str) -> bool:
        """Evict the oldest queued frame whose ``kind`` matches.

        Manipulates ``asyncio.Queue._queue`` directly. Safe under asyncio
        because this method is fully synchronous (no await points), and the
        deque is the documented backing store. ``qsize()`` reflects
        ``len(_queue)`` so deletion alone is sufficient bookkeeping for
        our use (we do not use ``join()``/``task_done()``).
        """
        if self._outbox is None:
            return False
        backing = self._outbox._queue  # type: ignore[attr-defined]
        for index, queued in enumerate(backing):
            if isinstance(queued, _OutboundFrame) and queued.kind == kind:
                del backing[index]
                return True
        return False


class ConnectionRegistry:
    """Tracks all active WebSocket connections."""

    def __init__(self) -> None:
        self._connections: dict[str, WsConnection] = {}
        self._unregister_listener: Callable[[WsConnection], None] | None = None

    def set_unregister_listener(self, listener: Callable[[WsConnection], None]) -> None:
        self._unregister_listener = listener

    def clear_unregister_listener(self, listener: Callable[[WsConnection], None]) -> None:
        if self._unregister_listener == listener:
            self._unregister_listener = None

    def register(self, conn: WsConnection) -> None:
        self._connections[conn.conn_id] = conn

    def unregister(self, conn_id: str) -> None:
        connection = self._connections.get(conn_id)
        try:
            if connection is not None and self._unregister_listener is not None:
                self._unregister_listener(connection)
        except Exception:
            log.warning("gateway.ws_unregister_listener_failed", exc_info=True)
        finally:
            self._connections.pop(conn_id, None)

    def get(self, conn_id: str) -> WsConnection | None:
        return self._connections.get(conn_id)

    def all(self) -> list[WsConnection]:
        return list(self._connections.values())

    async def broadcast(self, event: str, payload: Any = None) -> None:
        for conn in self.all():
            if conn.authenticated:
                try:
                    await conn.send_event(event, payload)
                except Exception:
                    pass


class SubscriptionManager:
    """Track which connections are subscribed to session-level and message-level events."""

    def __init__(self) -> None:
        self._session_subs: set[str] = set()  # conn_ids subscribed to session lifecycle
        self._message_subs: dict[str, set[str]] = {}  # session_key -> {conn_id}
        self._topic_subs: dict[str, set[str]] = {}  # topic -> {conn_id}
        self._message_unsubscribe_listener: Any | None = None
        # One token and one recovery revision per existing lease, not a second
        # unbounded set of global-dirty keys or historical ACK tombstones.
        self._message_subscription_tokens: dict[tuple[str, str], tuple[str, int]] = {}
        self._message_intents: dict[tuple[str, str], MessageSubscriptionIntent] = {}

    def get_message_intent(self, conn_id: str, key: str) -> MessageSubscriptionIntent | None:
        return self._message_intents.get((conn_id, key))

    def admit_message_subscription(
        self, conn_id: str, key: str,
    ) -> tuple[MessageSubscriptionIntent, bool]:
        existing = self._message_intents.get((conn_id, key))
        if existing is not None and not existing.closed:
            return existing, False
        active_token = self.get_message_subscription_token(conn_id, key)
        intent = MessageSubscriptionIntent(
            active_token or uuid.uuid4().hex, asyncio.get_running_loop().create_future(),
        )
        if active_token is not None:
            intent.ready.set_result(True)
        self._message_intents[(conn_id, key)] = intent
        return intent, active_token is None

    def activate_message_subscription(self, conn_id: str, key: str, token: object) -> bool:
        intent = self._message_intents.get((conn_id, key))
        if intent is None or intent.closed or intent.token != token:
            return False
        self._message_subs.setdefault(key, set()).add(conn_id)
        self._message_subscription_tokens.setdefault((conn_id, key), (intent.token, 0))
        if not intent.ready.done():
            intent.ready.set_result(True)
        return True

    def set_message_unsubscribe_listener(self, listener: Any | None) -> None:
        """Install a process-local observer for lost message subscriptions."""

        self._message_unsubscribe_listener = listener

    def _notify_message_unsubscribed(self, conn_id: str, session_key: str) -> None:
        listener = self._message_unsubscribe_listener
        if listener is None:
            return
        try:
            result = listener(conn_id, session_key)
            if inspect.isawaitable(result):
                asyncio.ensure_future(result)
        except Exception:
            log.warning(
                "subscription.message_unsubscribe_listener_failed",
                conn_id=conn_id,
                session_key=session_key,
                exc_info=True,
            )

    # -- session-level (sessions.subscribe / sessions.unsubscribe) --

    def subscribe_sessions(self, conn_id: str) -> None:
        self._session_subs.add(conn_id)

    def unsubscribe_sessions(self, conn_id: str) -> None:
        self._session_subs.discard(conn_id)

    def get_session_subscribers(self) -> set[str]:
        return set(self._session_subs)

    # -- message-level (sessions.messages.subscribe / unsubscribe) --

    def subscribe_messages(self, conn_id: str, session_key: str) -> None:
        self._message_subs.setdefault(session_key, set()).add(conn_id)
        self._message_subscription_tokens.setdefault((conn_id, session_key), (uuid.uuid4().hex, 0))

    def get_message_subscription_token(self, conn_id: str, session_key: str) -> str | None:
        lease = self._message_subscription_tokens.get((conn_id, session_key))
        return lease[0] if lease else None

    def get_message_flow_revision(self, conn_id: str, session_key: str) -> int:
        lease = self._message_subscription_tokens.get((conn_id, session_key))
        return lease[1] if lease else 0

    def set_message_flow_revision(self, conn_id: str, session_key: str, revision: int) -> None:
        lease = self._message_subscription_tokens.get((conn_id, session_key))
        if lease is not None:
            self._message_subscription_tokens[(conn_id, session_key)] = (lease[0], revision)

    def all_message_flow_revisions_match(self, conn_id: str, revision: int) -> bool:
        return all(
            lease[1] == revision
            for (owner, _), lease in self._message_subscription_tokens.items()
            if owner == conn_id
        )

    def unsubscribe_messages(
        self, conn_id: str, session_key: str, *, expected_token: object | None = None,
    ) -> None:
        intent = self._message_intents.get((conn_id, session_key))
        token = intent.token if intent is not None else self.get_message_subscription_token(
            conn_id, session_key,
        )
        if expected_token is not None and token != expected_token:
            return
        if intent is not None:
            self._message_intents.pop((conn_id, session_key), None)
            intent.retire()
        connection = get_registry().get(conn_id)
        if connection is not None:
            snapshots = getattr(connection, "_snapshot_registry", None)
            if snapshots is not None:
                snapshots.retire_lease(session_key, token)
            connection._retire_flow_subscription(session_key)
        if session_key in self._message_subs:
            removed = conn_id in self._message_subs[session_key]
            self._message_subs[session_key].discard(conn_id)
            if not self._message_subs[session_key]:
                del self._message_subs[session_key]
            if removed:
                self._message_subscription_tokens.pop((conn_id, session_key), None)
                clear_covered = getattr(connection, "_clear_covered_global_flow", None)
                if clear_covered is not None:
                    clear_covered()
                self._notify_message_unsubscribed(conn_id, session_key)

    def get_message_subscribers(self, session_key: str) -> set[str]:
        return set(self._message_subs.get(session_key, set()))

    # -- topic-level (cron.subscribe / cron.unsubscribe) --

    def subscribe_topic(self, conn_id: str, topic: str) -> None:
        self._topic_subs.setdefault(topic, set()).add(conn_id)

    def unsubscribe_topic(self, conn_id: str, topic: str) -> None:
        if topic in self._topic_subs:
            self._topic_subs[topic].discard(conn_id)
            if not self._topic_subs[topic]:
                del self._topic_subs[topic]

    def get_topic_subscribers(self, topic: str) -> set[str]:
        return set(self._topic_subs.get(topic, set()))

    def remove_connection(self, conn_id: str) -> None:
        """Clean up all subscriptions for a disconnected connection."""
        for (owner, key), intent in tuple(self._message_intents.items()):
            if owner == conn_id:
                intent.retire()
                del self._message_intents[(owner, key)]
        self._session_subs.discard(conn_id)
        removed_message_sessions: list[str] = []
        for session_key, subs in list(self._message_subs.items()):
            if conn_id in subs:
                subs.discard(conn_id)
                removed_message_sessions.append(session_key)
            if not subs:
                del self._message_subs[session_key]
        empty_topics = []
        for topic, subs in self._topic_subs.items():
            subs.discard(conn_id)
            if not subs:
                empty_topics.append(topic)
        for topic in empty_topics:
            del self._topic_subs[topic]
        for session_key in removed_message_sessions:
            self._message_subscription_tokens.pop((conn_id, session_key), None)
            self._notify_message_unsubscribed(conn_id, session_key)


# Module-level registry shared across connections
_registry = ConnectionRegistry()


def get_registry() -> ConnectionRegistry:
    return _registry


def _is_wire_text(value: str) -> bool:
    """True when ``value`` can be re-serialized onto the wire as UTF-8.

    Valid JSON may carry lone-surrogate escapes (``"\\ud800"``); echoing one
    into a response frame makes ``model_dump_json`` raise at send time, long
    after the handler ran.
    """
    return is_utf8_encodable(value)


def _wire_frame_id(raw_id: Any, fallback: str = "") -> str:
    """Best-effort string id for correlating a response to a client frame.

    ``ResFrame.id`` must be a string, but a client may send any JSON value.
    Scalar ids are echoed back stringified so the client can still correlate
    the error; container and non-encodable ids fall back to ``fallback``.
    """
    if isinstance(raw_id, str):
        return raw_id if _is_wire_text(raw_id) else fallback
    if isinstance(raw_id, bool | int | float):
        return str(raw_id)
    return fallback


async def handle_ws_connection(
    ws: WebSocket,
    config: GatewayConfig,
    dispatcher: RpcDispatcher,
    session_manager: Any = None,
    provider_selector: Any = None,
    tool_registry: Any = None,
    subscription_manager: Any = None,
    channel_manager: Any = None,
    usage_tracker: Any = None,
    usage_event_sink: Any = None,
    meta_run_writer: Any = None,
    skill_loader: Any = None,
    skill_management_state: dict[str, Any] | None = None,
    cron_scheduler: Any = None,
    turn_runner: Any = None,
    task_runtime: Any = None,
    heartbeat_service: Any = None,
    heartbeat_loop: Any = None,
    agent_registry: Any = None,
    diagnostics_state: Any = None,
    provider_stats: Any = None,
    memory_managers: dict[str, Any] | None = None,
    memory_stores: dict[str, Any] | None = None,
    memory_retrievers: dict[str, Any] | None = None,
    prompt_cache_keepalive_service: Any = None,
    skill_management_service: Any = None,
    artifact_preview_service: Any = None,
) -> None:
    """Main WebSocket connection handler."""
    if not websocket_origin_allowed(ws, config):
        log.warning(
            "gateway.origin_rejected",
            category="websocket_cross_origin",
        )
        await ws.close(code=1008)
        return

    conn_id = str(uuid.uuid4())
    conn = WsConnection(conn_id=conn_id, ws=ws)
    registry = get_registry()

    await ws.accept()
    log.info("ws.connected", conn_id=conn_id, remote=str(ws.client))

    # Step 1: Send connect.challenge
    nonce = str(uuid.uuid4())
    try:
        await conn.send_event("connect.challenge", {"nonce": nonce})
    except (WebSocketDisconnect, TimeoutError):
        return

    # Step 2: Pre-auth timeout — client must send connect request
    try:
        preauth_timeout = PREAUTH_TIMEOUT_MS / 1000
        raw = await asyncio.wait_for(ws.receive_text(), timeout=preauth_timeout)
    except TimeoutError:
        log.warning("ws.preauth_timeout", conn_id=conn_id)
        await conn.close()
        return
    except WebSocketDisconnect:
        return

    # Step 3: Parse the connect request
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        # ValueError covers JSONDecodeError plus non-decode parse failures
        # (e.g. the int-digit conversion limit); RecursionError covers
        # pathological nesting depth.
        await conn.send_res(
            make_error_res("handshake", "INVALID_REQUEST", "Invalid JSON in connect frame")
        )
        await conn.close()
        return
    if not isinstance(data, dict):
        await conn.send_res(
            make_error_res("handshake", "INVALID_REQUEST", "Connect frame must be a JSON object")
        )
        await conn.close()
        return

    try:
        validate_rpc_ingress(data)
    except RpcIngressValidationError as exc:
        await conn.send_res(
            make_error_res(
                _wire_frame_id(data.get("id"), "handshake"),
                "INVALID_REQUEST",
                str(exc),
                details={"reason": exc.reason},
            )
        )
        await conn.close()
        return

    if data.get("type") != "req" or data.get("method") != "connect":
        await conn.send_res(
            make_error_res(
                _wire_frame_id(data.get("id"), "handshake"),
                "INVALID_REQUEST",
                "First message must be connect request",
            )
        )
        await conn.close()
        return

    req_id = _wire_frame_id(data.get("id"), "handshake")
    params_raw = data.get("params")
    if not isinstance(params_raw, dict):
        params_raw = {}

    # Step 4: Resolve auth via server-side ScopeResolver
    from opensquilla.gateway.auth import resolve_auth

    auth_params = params_raw.get("auth")
    if not isinstance(auth_params, dict):
        auth_params = {}
    role_claim = params_raw.get("role", "operator")
    if not isinstance(role_claim, str):
        role_claim = "operator"
    peer_ip = ws.client.host if ws.client is not None else None
    principal = resolve_auth(
        config,
        auth_params=auth_params,
        role_claim=role_claim,
        peer_ip=peer_ip,
    )
    if principal is None:
        await conn.send_res(make_error_res(req_id, "UNAUTHORIZED", "Authentication failed"))
        await conn.close()
        return
    if principal.auth_state == "invalid":
        from opensquilla.gateway.token_store import default_auth_failure_limiter

        await default_auth_failure_limiter().wait_after_failure(
            peer_ip,
            principal.token_public_id,
        )
        log.warning(
            "ws.auth_invalid_guest_only",
            conn_id=conn_id,
            peer_ip=peer_ip,
            token_public_id=principal.token_public_id,
        )

    # Step 5: Negotiate protocol version
    min_proto = params_raw.get("minProtocol", 1)
    max_proto = params_raw.get("maxProtocol", PROTOCOL_VERSION)
    if not all(
        isinstance(bound, int) and not isinstance(bound, bool) for bound in (min_proto, max_proto)
    ):
        await conn.send_res(
            make_error_res(
                req_id, "INVALID_REQUEST", "minProtocol and maxProtocol must be integers"
            )
        )
        await conn.close()
        return
    negotiated = min(max_proto, PROTOCOL_VERSION)
    if negotiated < min_proto:
        await conn.send_res(
            make_error_res(req_id, "INVALID_REQUEST", "Unsupported protocol version range")
        )
        await conn.close()
        return

    # Assign principal
    conn.principal = principal
    conn.protocol = negotiated
    requested_caps = params_raw.get("caps")
    conn.client_caps = frozenset(
        capability
        for capability in (requested_caps[:128] if isinstance(requested_caps, list) else ())
        if isinstance(capability, str) and capability and len(capability) <= 128
    )
    conn._subscriptions = subscription_manager
    conn._recovery_enabled = bool(
        RECOVERY_CAPABILITY in conn.client_caps
        and FLOW_CAPABILITY in conn.client_caps
        and config.ws_transport_flow_enabled
        and config.ws_writer_queue_enabled
        and {"sessions.messages.resume", "sessions.messages.snapshot.release"}.issubset(
            dispatcher.list_methods()
        )
    )
    if (
        config.ws_transport_flow_enabled
        and config.ws_writer_queue_enabled
        and FLOW_CAPABILITY in conn.client_caps
        and {"transport.flow.update", "sessions.messages.snapshot.read"}.issubset(
            dispatcher.list_methods()
        )
    ):
        conn._enable_flow()

    # Step 6: Send HelloOk
    hello = HelloOk(
        protocol=negotiated,
        server=ServerInfo(version=__version__, conn_id=conn_id),
        features=_build_features(dispatcher),
        snapshot=SnapshotInfo(
            uptime_ms=int(time.time() * 1000),
            config_path=config.config_path,
            state_dir=config.state_dir,
            auth_mode=config.auth.mode,
        ),
        policy=PolicyInfo(
            transport_probe_nonce=PROBE_CAPABILITY in conn.client_caps,
            transport_flow=(
                {
                    "delivery_epoch": conn._flow.epoch,
                    "window_frames": FLOW_WINDOW_FRAMES,
                    "window_bytes": FLOW_WINDOW_BYTES,
                }
                if conn._flow is not None
                else None
            ),
            concurrent_history_reads=True,
            chat_send_initial_model=True,
            sessions_routing_model_selection=True,
            concurrent_optional_read_methods=sorted(_CONCURRENT_OPTIONAL_READ_METHODS),
            cancellable_request_methods=sorted(
                _CANCELLABLE_REQUEST_METHODS | (
                    _RECOVERY_READ_METHODS if conn._recovery_enabled else frozenset()
                )
            ),
            provider_probe_modes=list(_PROVIDER_PROBE_MODES),
            agent_stream_heartbeat_interval_ms=int(
                max(0.0, float(getattr(config, "agent_stream_heartbeat_interval_seconds", 15.0)))
                * 1000
            ),
            agent_stream_idle_timeout_ms=int(
                effective_agent_stream_idle_timeout_seconds(config) * 1000
            ),
            webui_stream_idle_grace_ms=int(
                effective_webui_stream_idle_grace_seconds(config) * 1000
            ),
            client_ws_keepalive_timeout_ms=int(
                max(0.0, float(getattr(config, "client_ws_keepalive_timeout_s", 0.0))) * 1000
            ),
        ),
        auth=_websocket_hello_auth_payload(principal),
    )
    try:
        await conn.send_raw_text(
            hello.model_dump_json(
                exclude={"policy": {"transport_flow"}} if conn._flow is None else None,
            )
        )
    except (WebSocketDisconnect, TimeoutError):
        return

    registry.register(conn)
    # Boundary: pre-auth direct-send ends here. After registry.register(conn),
    # conn._writer_task owns all post-auth sends. send_event/send_res route
    # through conn._outbox; WS-frame seq is minted at dequeue inside the
    # writer loop (NOT at enqueue), so dropped lossy frames never consume a
    # seq number.
    # Kill switch (config.ws_writer_queue_enabled) is read here at registration
    # time only — affects new connections only; existing connections retain
    # their startup-time behavior.
    conn._start_writer(
        maxsize=config.ws_writer_queue_maxsize,
        enabled=config.ws_writer_queue_enabled,
    )
    log.info("ws.authenticated", conn_id=conn_id, role=conn.role)

    # Step 7: Main message loop
    tick_task = asyncio.create_task(_tick_loop(conn, hello.policy.tick_interval_ms))
    try:
        await _message_loop(
            conn,
            config,
            dispatcher,
            session_manager,
            provider_selector,
            tool_registry,
            subscription_manager,
            channel_manager,
            usage_tracker,
            usage_event_sink,
            meta_run_writer,
            skill_loader,
            skill_management_state,
            cron_scheduler,
            turn_runner,
            task_runtime,
            heartbeat_service,
            heartbeat_loop,
            agent_registry,
            diagnostics_state,
            memory_managers,
            memory_stores,
            memory_retrievers,
            provider_stats=provider_stats,
            prompt_cache_keepalive_service=prompt_cache_keepalive_service,
            skill_management_service=skill_management_service,
            artifact_preview_service=artifact_preview_service,
        )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.error("ws.error", conn_id=conn_id, error=str(exc))
    finally:
        # Stop admission before draining. A running mutation may finish under
        # supervision; queued work must never begin after the client has left.
        conn._closing = True
        get_recovery_scheduler().cancel_connection(conn.conn_id)
        await conn._stop_ordinary_requests()
        # Detached optional reads must stop before the writer so a handler that
        # suppresses cancellation cannot enqueue a late response after teardown.
        await conn._stop_detached_requests()
        # Detached reads can still enqueue responses, so retire them before the
        # writer. Then stop the writer before tick_task.cancel() and before
        # registry.unregister. Otherwise a producer could still hold a reference
        # to this connection while the writer is mid-cancel.
        await conn._stop_detached_reads()
        await conn._stop_writer()
        tick_task.cancel()
        try:
            await tick_task
        except asyncio.CancelledError:
            pass
        registry.unregister(conn_id)
        conn._cleanup_transport()
        if subscription_manager is not None:
            subscription_manager.remove_connection(conn_id)
        log.info("ws.disconnected", conn_id=conn_id)


def _websocket_hello_auth_payload(principal: Any) -> dict[str, Any]:
    """Add the browser guest credential only to anonymous WebSocket hellos."""

    from opensquilla.sandbox.run_mode_policy import hello_auth_payload

    payload = hello_auth_payload(principal)
    payload["principal"]["guestOwnerId"] = getattr(principal, "guest_owner_id", None)
    guest_session_key = getattr(principal, "guest_session_key", None)
    if guest_session_key and not getattr(principal, "authenticated", False):
        # Preserve ``invalid`` and the public id internally for rate limiting,
        # but expose exactly the same anonymous authority as a missing token.
        payload["principal"]["authState"] = "guest"
        payload["principal"]["tokenPublicId"] = None
        payload["guestSessionKey"] = guest_session_key
    return payload


async def _tick_loop(conn: WsConnection, tick_interval_ms: int) -> None:
    interval_s = max(1.0, tick_interval_ms / 1000)
    next_sample = time.monotonic() + 60.0
    worst_lag_ms = 0.0
    while True:
        expected_wake = time.monotonic() + interval_s
        await asyncio.sleep(interval_s)
        now = time.monotonic()
        worst_lag_ms = max(worst_lag_ms, max(0.0, now - expected_wake) * 1000)
        if now >= next_sample:
            log.debug(
                "gateway.ws_transport_sample",
                conn_id=conn.conn_id,
                event_loop_lag_ms=round(worst_lag_ms, 1),
                **conn.transport_diagnostics(),
            )
            next_sample = now + 60.0
            worst_lag_ms = 0.0
        try:
            await conn.send_event("tick", {"time_ms": int(time.time() * 1000)})
        except Exception:
            log.debug("ws.tick_failed", conn_id=conn.conn_id, exc_info=True)
            return


async def _dispatch_request(
    conn: WsConnection,
    dispatcher: RpcDispatcher,
    req_id: str,
    method: str,
    params: Any,
    ctx: RpcContext,
) -> None:
    try:
        res = await dispatcher.dispatch(req_id, method, params, ctx)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("gateway.ws_request_failed", conn_id=conn.conn_id, method=method)
        res = make_error_res(req_id, "INTERNAL_ERROR", "Request failed")
    operation = CURRENT_RECOVERY_OPERATION.get()
    if operation is None or operation.current() or (not res.ok and not operation.closed):
        await conn.send_res(res, transport_control=(
            method in _CONTROL_RPC_METHODS or method in _RECOVERY_CONTROL_METHODS
            or method == "sessions.messages.resume"
        ))


def _authorized_admission_params(
    dispatcher: RpcDispatcher, method: str, params: Any, ctx: RpcContext,
) -> dict[str, Any] | None:
    """Authorize before creating intents or invalidating an existing owner."""
    from opensquilla.gateway.adapters.connection_recovery_contract import validate_recovery_params
    from opensquilla.gateway.guest_rpc_policy import GuestRpcPolicy, GuestRpcPolicyError
    from opensquilla.gateway.scopes import authorize_call

    entry = dispatcher.get_entry(method) if hasattr(dispatcher, "get_entry") else None
    if entry is None:
        return None
    try:
        params = GuestRpcPolicy.authorize(method, params, ctx)
    except GuestRpcPolicyError:
        return None
    allowed, _ = authorize_call(method, entry.required_scope, ctx.role, ctx.principal.scopes)
    if not allowed:
        return None
    if entry.generated_contract_name is not None:
        validate_recovery_params(method, params)
    return params if isinstance(params, dict) else None


async def _dispatch_mutation(
    predecessors: tuple[asyncio.Future[None], ...],
    conn: WsConnection, dispatcher: RpcDispatcher, req_id: str,
    method: str, params: Any, ctx: RpcContext,
) -> None:
    for predecessor in predecessors:
        await asyncio.shield(predecessor)
    await _dispatch_request(conn, dispatcher, req_id, method, params, ctx)


def _admit_session_mutation(
    method: str, params: dict[str, Any], ctx: RpcContext,
) -> tuple[tuple[asyncio.Future[None], ...], asyncio.Future[None] | None]:
    from opensquilla.gateway.session_services import get_session_storage
    from opensquilla.session.keys import canonicalize_session_key

    if method == "sessions.delete" and "keys" in params:
        raw_keys = params["keys"]
        if not isinstance(raw_keys, list | tuple) or not all(
            isinstance(key, str) for key in raw_keys
        ):
            return (), None
    else:
        raw_keys = [params.get("key", params.get("sessionKey"))]
    try:
        keys = {canonicalize_session_key(key) for key in raw_keys if isinstance(key, str)}
    except ValueError:
        return (), None
    if not keys:
        return (), None
    runtime = get_session_storage(ctx.session_manager) or ctx.session_manager or get_registry()
    predecessors = []
    tails = []
    scheduler = get_recovery_scheduler()
    for key in keys:
        predecessor, tail = scheduler.admit_mutation(runtime, key)
        if predecessor is not None:
            predecessors.append(predecessor)
        tails.append(tail)
        if method in _SESSION_IDENTITY_MUTATIONS:
            for affected in get_registry().all():
                if affected._recovery_runtime is not runtime:
                    continue
                affected._resume_proofs.pop(key, None)
                if affected._snapshot_registry is not None:
                    lease = affected._subscriptions.get_message_subscription_token(
                        affected.conn_id, key,
                    ) if affected._subscriptions is not None else None
                    affected._snapshot_registry.retire_lease(key, lease)
                for operation in tuple(affected._recovery_operations.values()):
                    if operation.key == key:
                        scheduler.cancel(operation)
                if affected._flow is not None:
                    affected._mark_flow_dirty({"session_key": key})
    completion: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def complete(_: asyncio.Future[None]) -> None:
        for tail in tails:
            if not tail.done():
                tail.set_result(None)

    completion.add_done_callback(complete)
    return tuple(predecessors), completion


async def _message_loop(
    conn: WsConnection,
    config: GatewayConfig,
    dispatcher: RpcDispatcher,
    session_manager: Any,
    provider_selector: Any = None,
    tool_registry: Any = None,
    subscription_manager: Any = None,
    channel_manager: Any = None,
    usage_tracker: Any = None,
    usage_event_sink: Any = None,
    meta_run_writer: Any = None,
    skill_loader: Any = None,
    skill_management_state: dict[str, Any] | None = None,
    cron_scheduler: Any = None,
    turn_runner: Any = None,
    task_runtime: Any = None,
    heartbeat_service: Any = None,
    heartbeat_loop: Any = None,
    agent_registry: Any = None,
    diagnostics_state: Any = None,
    memory_managers: dict[str, Any] | None = None,
    memory_stores: dict[str, Any] | None = None,
    memory_retrievers: dict[str, Any] | None = None,
    provider_stats: Any = None,
    prompt_cache_keepalive_service: Any = None,
    skill_management_service: Any = None,
    artifact_preview_service: Any = None,
) -> None:
    ws = conn.ws
    keepalive_timeout = max(0.0, float(getattr(config, "client_ws_keepalive_timeout_s", 0.0)))
    while not conn._closing:
        try:
            if keepalive_timeout > 0.0:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=keepalive_timeout)
            else:
                raw = await ws.receive_text()
        except WebSocketDisconnect:
            return
        except RuntimeError:
            if (
                conn._closing
                or ws.application_state != WebSocketState.CONNECTED
                or ws.client_state != WebSocketState.CONNECTED
            ):
                return
            raise
        except TimeoutError:
            log.warning(
                "gateway.client_ws_keepalive_timeout",
                conn_id=conn.conn_id,
                timeout_s=keepalive_timeout,
            )
            await conn._stop_writer()
            await conn.close(code=1011)
            return

        try:
            raw_size = len(raw.encode("utf-8"))
        except UnicodeEncodeError:
            await conn.send_res(
                make_error_res(
                    "",
                    "INVALID_REQUEST",
                    "RPC request contains text that cannot be encoded as UTF-8",
                    details={"reason": "invalid_utf8_text"},
                )
            )
            continue
        try:
            if raw_size > MAX_PAYLOAD_BYTES:
                await conn.send_res(
                    make_error_res("", "PAYLOAD_TOO_LARGE", "Frame exceeds the wire limit")
                )
                continue
            data = json.loads(raw)
        except (ValueError, RecursionError):
            # ValueError covers JSONDecodeError plus non-decode parse failures
            # (e.g. the int-digit conversion limit); RecursionError covers
            # pathological nesting depth.
            await conn.send_res(make_error_res("", "INVALID_REQUEST", "Invalid JSON"))
            continue
        if not isinstance(data, dict):
            await conn.send_res(
                make_error_res("", "INVALID_REQUEST", "Frame must be a JSON object")
            )
            continue

        try:
            validate_rpc_ingress(data)
        except RpcIngressValidationError as exc:
            await conn.send_res(
                make_error_res(
                    _wire_frame_id(data.get("id")),
                    "INVALID_REQUEST",
                    str(exc),
                    details={"reason": exc.reason},
                )
            )
            continue

        frame_type = data.get("type")

        if frame_type == "cancel":
            request_id = data.get("id")
            if (
                set(data) != {"type", "id"}
                or not isinstance(request_id, str)
                or not request_id
                or not _is_wire_text(request_id)
            ):
                await conn.send_res(
                    make_error_res(
                        _wire_frame_id(request_id),
                        "INVALID_REQUEST",
                        "Cancel frame must contain only a non-empty string id",
                    )
                )
                continue
            # Cancellation is intentionally connection-local and idempotent.
            # There is no acknowledgement because the client has already
            # retired the pending request that owned this id.
            conn._cancel_detached_request(request_id)
            operation = conn._recovery_operations.get(request_id)
            if operation is not None:
                get_recovery_scheduler().cancel(operation)
            continue

        if frame_type == "ping":
            nonce = data.get("nonce")
            if nonce is not None and (
                not isinstance(nonce, str)
                or not 1 <= len(nonce) <= 64
                or not all(" " <= character <= "~" for character in nonce)
            ):
                await conn.send_res(make_error_res("", "INVALID_REQUEST", "Invalid probe nonce"))
                continue
            pong: dict[str, Any] = {"type": "pong"}
            if nonce is not None and PROBE_CAPABILITY in conn.client_caps:
                pong["nonce"] = nonce
            await conn.send_raw_text(json.dumps(pong, separators=(",", ":")))
            continue

        if frame_type == "pong":
            continue

        if frame_type == "req":
            req_id = data.get("id", "")
            method = data.get("method", "")
            if (
                not isinstance(req_id, str)
                or not isinstance(method, str)
                or not _is_wire_text(req_id)
                or not _is_wire_text(method)
            ):
                # A non-string or non-encodable id/method would fail ResFrame
                # validation or serialization after the handler already ran;
                # reject the frame here so one malformed request cannot kill
                # the connection.
                await conn.send_res(
                    make_error_res(
                        _wire_frame_id(req_id),
                        "INVALID_REQUEST",
                        "Frame id and method must be UTF-8-encodable strings",
                    )
                )
                continue
            params = data.get("params")

            ctx = RpcContext(
                conn_id=conn.conn_id,
                principal=conn.principal,
                protocol=conn.protocol,
                sandbox_schema_version=2 if conn.protocol >= 4 else 1,
                session_manager=session_manager,
                config=config,
                provider_selector=provider_selector,
                tool_registry=tool_registry,
                subscription_manager=subscription_manager,
                # Live reconcile can create the manager after this connection
                # opened; a callable is re-resolved per request so long-lived
                # console sockets see it.
                channel_manager=(
                    channel_manager() if callable(channel_manager) else channel_manager
                ),
                usage_tracker=usage_tracker,
                usage_event_sink=usage_event_sink,
                meta_run_writer=meta_run_writer,
                skill_loader=skill_loader,
                skill_management_service=skill_management_service,
                skill_management_state=(
                    skill_management_state if skill_management_state is not None else {}
                ),
                cron_scheduler=cron_scheduler,
                turn_runner=turn_runner,
                task_runtime=task_runtime,
                heartbeat_service=heartbeat_service,
                heartbeat_loop=heartbeat_loop,
                prompt_cache_keepalive_service=prompt_cache_keepalive_service,
                agent_registry=agent_registry,
                diagnostics_state=diagnostics_state,
                provider_stats=provider_stats,
                memory_managers=memory_managers or {},
                memory_stores=memory_stores or {},
                memory_retrievers=memory_retrievers or {},
                artifact_preview_service=artifact_preview_service,
            )
            mutation_predecessor: tuple[asyncio.Future[None], ...] = ()
            mutation_completion = None
            legacy_resume = bool(
                method == "transport.flow.update" and isinstance(params, dict)
                and params.get("resume")
            )
            if (conn._recovery_enabled and method in (
                _RECOVERY_READ_METHODS | _RECOVERY_CONTROL_METHODS
            )) or legacy_resume:
                try:
                    admitted_params = _authorized_admission_params(dispatcher, method, params, ctx)
                except (ValueError, KeyError):
                    await conn.send_res(make_error_res(
                        req_id, "INVALID_REQUEST", "Invalid recovery request parameters",
                        accepted=False,
                    ))
                    continue
                if admitted_params is None:
                    await _dispatch_request(conn, dispatcher, req_id, method, params, ctx)
                    continue
                params = admitted_params
                if method in _RECOVERY_READ_METHODS or legacy_resume:
                    if not conn._try_recovery_request(
                        dispatcher, req_id, method, params, ctx, raw_size,
                    ):
                        await conn.send_res(make_error_res(
                            req_id, "STORAGE_BUSY", "Recovery queue is full",
                            retryable=True, retry_after_ms=100, accepted=False,
                        ))
                    await asyncio.sleep(0)
                    continue
                if method in _RECOVERY_CONTROL_METHODS:
                    await _dispatch_request(conn, dispatcher, req_id, method, params, ctx)
                    continue
            if method in _SESSION_MUTATION_METHODS:
                try:
                    mutation_params = _authorized_admission_params(dispatcher, method, params, ctx)
                except (ValueError, KeyError):
                    mutation_params = None
                if mutation_params is not None:
                    mutation_predecessor, mutation_completion = _admit_session_mutation(
                        method, mutation_params, ctx,
                    )
            if method in _CONTROL_RPC_METHODS:
                await _dispatch_request(conn, dispatcher, req_id, method, params, ctx)
                continue
            if _should_detach_rpc_request(method, params):
                cancellable = _is_cancellable_request(method, params)
                if cancellable and req_id in conn._cancellable_request_tasks:
                    await conn.send_res(
                        make_error_res(
                            req_id,
                            "INVALID_REQUEST",
                            "A cancellable request with this id is already running",
                        )
                    )
                    continue
                ordinary_detached_count = (
                    len(conn._detached_request_tasks)
                    - len(conn._cancellable_request_tasks)
                )
                at_limit = (
                    len(conn._cancellable_request_tasks)
                    >= _MAX_CANCELLABLE_REQUESTS_PER_CONNECTION
                    if cancellable
                    else ordinary_detached_count >= _MAX_DETACHED_REQUESTS_PER_CONNECTION
                )
                provider_probe_lease = None
                if cancellable and not at_limit:
                    provider_probe_lease = _try_acquire_provider_probe_lease()
                    at_limit = provider_probe_lease is None
                if at_limit:
                    await conn.send_res(
                        make_error_res(
                            req_id,
                            ERROR_UNAVAILABLE,
                            (
                                "Too many provider probe requests are already running"
                                if cancellable
                                else "Too many detached requests are already running"
                            ),
                            retryable=True,
                        )
                    )
                    continue
                task = asyncio.create_task(
                    _dispatch_and_send(
                        conn,
                        dispatcher,
                        req_id,
                        method,
                        params,
                        ctx,
                        detached=True,
                        cancellable=cancellable,
                    ),
                    name=f"ws-detached-request-{conn.conn_id}",
                )
                conn._track_detached_request(
                    task,
                    request_id=req_id if cancellable else None,
                    provider_probe_lease=provider_probe_lease,
                )
                continue
            provider_probe_lease = None
            if _is_provider_probe_request(method):
                provider_probe_lease = _try_acquire_provider_probe_lease()
                if provider_probe_lease is None:
                    await conn.send_res(
                        make_error_res(
                            req_id,
                            ERROR_UNAVAILABLE,
                            "Too many provider probe requests are already running",
                            retryable=True,
                        )
                    )
                    continue
            request = (
                _dispatch_mutation(
                    mutation_predecessor, conn, dispatcher, req_id, method, params, ctx,
                ) if mutation_completion is not None else
                _dispatch_request(conn, dispatcher, req_id, method, params, ctx)
            )
            if method in _DETACHED_READ_METHODS:
                if conn._try_start_detached_read(request, method=method):
                    # History reads may wait on storage while the client still
                    # needs the same connection for navigation and controls.
                    continue
                request.close()
                await conn.send_res(
                    make_error_res(
                        req_id,
                        "STORAGE_BUSY",
                        "Too many history reads are already in progress",
                        retryable=True,
                        retry_after_ms=100,
                    )
                )
                continue
            if not conn._enqueue_ordinary_request(
                request,
                raw_size,
                provider_probe_lease=provider_probe_lease,
                mutation_completion=mutation_completion,
            ):
                request.close()
                if mutation_completion is not None and not mutation_completion.done():
                    mutation_completion.set_result(None)
                if provider_probe_lease is not None:
                    _release_provider_probe_lease(provider_probe_lease)
                await conn.send_res(
                    make_error_res(
                        req_id,
                        ERROR_UNAVAILABLE,
                        "Connection request queue is full",
                        retryable=True,
                        retry_after_ms=100,
                        accepted=False,
                    )
                )
            # Admit/execute fast requests promptly while keeping slow handlers
            # independent of subsequent ping, cancel and flow control ingress.
            await asyncio.sleep(0)
        else:
            # repr keeps the echo serializable for any client value (lone
            # surrogates escape to backslash form).
            await conn.send_res(
                make_error_res("", "INVALID_REQUEST", f"Unknown frame type: {frame_type!r}")
            )


async def _dispatch_and_send(
    conn: WsConnection,
    dispatcher: RpcDispatcher,
    req_id: str,
    method: str,
    params: Any,
    ctx: RpcContext,
    *,
    detached: bool = False,
    cancellable: bool = False,
) -> None:
    response = await dispatcher.dispatch(req_id, method, params, ctx)
    if detached and not conn._accept_detached_responses:
        return
    if cancellable and not conn._detached_response_allowed(asyncio.current_task()):
        return
    await conn.send_res(response)


def _build_features(dispatcher: RpcDispatcher) -> Any:
    from opensquilla.contracts.gateway_transport import TURN_COMMITTED_EVENT
    from opensquilla.gateway.protocol import FeaturesInfo

    methods = dispatcher.list_methods()
    events = [
        "connect.challenge",
        "agent",
        "session.message",
        "sessions.changed",
        "presence",
        "tick",
        "shutdown",
        "health",
        "heartbeat",
        "cron",
        TURN_COMMITTED_EVENT,
    ]
    return FeaturesInfo(methods=methods, events=events)
