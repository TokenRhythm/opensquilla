"""TUI surface adapter backed by the OpenTUI footer host."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog

from opensquilla.cli.tui.backend.contracts import TuiSubmittedInput, TuiSurface
from opensquilla.cli.tui.opentui.bridge import OpenTuiBridge
from opensquilla.cli.tui.opentui.completion import (
    build_completion_context,
    enumerate_workspace_files,
)
from opensquilla.cli.tui.opentui.context import context_update_from_bootstrap
from opensquilla.cli.tui.opentui.messages import (
    ApprovalDismiss,
    AttachmentClear,
    AttachmentRemove,
    AttachmentState,
    AttachmentUpdate,
    CompletionContext,
    ComposerState,
    ContextUpdate,
    HistoryReplace,
    HostApprovalResponse,
    HostCompletionRequest,
    HostError,
    HostInputCancel,
    HostInputEof,
    HostInputSubmit,
    HostProtocolUnknown,
    HostReady,
    HostResize,
    HostThemeSelected,
    RouterPluginState,
    ScrollbackWrite,
)
from opensquilla.engine.commands import Surface

log = structlog.get_logger(__name__)

# The host recovers from per-message failures and reports them as diagnostic
# error frames, so one frame must never end the session. A run of consecutive
# error frames with nothing else in between means every dispatch is failing —
# a genuinely broken host — and only then is teardown warranted.
_MAX_CONSECUTIVE_HOST_ERROR_FRAMES = 8

_ROUTER_LABEL_RE = re.compile(
    r"^(?P<mode>route|forced|observe)\s+(?P<tier>\S+)\s+->\s+"
    r"(?P<model>\S+)(?:\s+(?P<confidence>\d+%))?"
    r"(?:\s+save\s+(?P<saving>\d+%))?"
)
_FALLBACK_LABEL_RE = re.compile(r"^fallback\s+->\s+(?P<model>\S+)")

# How long an approval overlay may sit unanswered before the request is
# treated as denied. Generous on purpose: the user may be reading the tool
# summary, but a turn must never hang forever on a forgotten prompt.
_APPROVAL_RESPONSE_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class ExternalApprovalResolution:
    """Approval decision completed by another control surface."""

    id: str
    approved: bool
    resolution: str | None = None
    resolved_externally: bool = True


_ATTACHMENT_STATUSES = frozenset({"reading", "uploading", "ready", "failed"})


class _OpenTuiBridgeLike(Protocol):
    async def send(self, message_type: str, payload: object | None = None) -> None: ...

    async def next_message(self) -> object | None: ...


def _sanitized_approval_payload(request: dict[str, object]) -> dict[str, object]:
    """Copy an approval.request payload with its display text made cell-safe.

    This is the single choke point for every overlay payload — the canonical
    tool-request path AND gateway-push previews — so tool/model-derived text
    cannot smuggle control bytes into the host's raw cell renderer no matter
    which caller built the dict.
    """
    from opensquilla.cli.tui.backend.render_summary import (  # noqa: PLC0415
        sanitize_terminal_text,
    )

    payload = dict(request)
    for key in ("summary", "message", "tool"):
        value = payload.get(key)
        if isinstance(value, str):
            payload[key] = sanitize_terminal_text(value)
    return payload


class OpenTuiOutputHandle:
    """Typed output handle that writes transcript data through OpenTUI scrollback."""

    def __init__(
        self,
        bridge: _OpenTuiBridgeLike,
        *,
        approval_surface: Surface,
    ) -> None:
        self._bridge = bridge
        self.approval_surface = approval_surface
        self._toolbar: dict[str, object] = {}
        # Waiters keyed by approval id. The registry lives on the output handle
        # (shared by the surface and the stream renderer, and stable across
        # next_line task recreation) so a pending overlay answer is never lost
        # when the runtime cancels and re-creates its input task.
        self._approval_waiters: dict[
            str,
            asyncio.Future[HostApprovalResponse | ExternalApprovalResolution | None],
        ] = {}
        # Last payload shown for each approval. Gateway push events can arrive
        # before the canonical tool request and may intentionally contain only
        # a summary. Keep the rendered payload so the richer request can
        # replace the existing overlay instead of silently losing choices such
        # as ``allow_same_type``.
        self._approval_requests: dict[str, dict[str, object]] = {}
        self._resolved_gateway_approvals: dict[str, ExternalApprovalResolution] = {}
        self._attachment_states: dict[str, AttachmentState] = {}
        self._attachment_seq = 0
        self._attachment_lock = asyncio.Lock()

    async def write_through(self, payload: str) -> None:
        await self._bridge.send("scrollback.write", ScrollbackWrite(text=payload))

    async def send_message(self, message_type: str, payload: dict[str, object]) -> None:
        await self._bridge.send(message_type, payload)

    async def add_attachment(
        self,
        *,
        kind: str,
        label: str,
        status: str = "reading",
        message: str = "",
        attachment_id: str | None = None,
    ) -> str:
        """Add a chip and return its stable id for later updates/removal."""

        normalized_status = _attachment_status(status)
        safe_kind = _attachment_kind(kind)
        safe_label = _attachment_label(label)
        safe_message = _attachment_message(message)
        async with self._attachment_lock:
            # A failed retry replaces the old chip. A pending duplicate reuses
            # its id so two callers cannot create two sends for one file.
            for current in tuple(self._attachment_states.values()):
                if current.kind != safe_kind or current.label != safe_label:
                    continue
                if current.status in {"reading", "uploading"}:
                    return current.id
                if current.status == "failed":
                    await self._bridge.send(
                        "attachment.remove",
                        AttachmentRemove(id=current.id),
                    )
                    self._attachment_states.pop(current.id, None)
            self._attachment_seq += 1
            safe_id = _attachment_id(attachment_id or f"attachment-{self._attachment_seq}")
            state = AttachmentState(
                id=safe_id,
                kind=safe_kind,
                label=safe_label,
                status=normalized_status,
                message=safe_message,
            )
            self._attachment_states[safe_id] = state
            try:
                await self._bridge.send("attachment.add", state)
            except Exception:
                self._attachment_states.pop(safe_id, None)
                raise
            return safe_id

    async def update_attachment(
        self,
        attachment_id: str,
        *,
        status: str,
        message: str = "",
    ) -> bool:
        """Update a chip; return False when it has already been removed."""

        safe_id = _attachment_id(attachment_id)
        normalized_status = _attachment_status(status)
        safe_message = _attachment_message(message)
        async with self._attachment_lock:
            current = self._attachment_states.get(safe_id)
            if current is None:
                return False
            updated = AttachmentState(
                id=current.id,
                kind=current.kind,
                label=current.label,
                status=normalized_status,
                message=safe_message,
            )
            self._attachment_states[safe_id] = updated
            try:
                await self._bridge.send(
                    "attachment.update",
                    AttachmentUpdate(
                        id=safe_id,
                        status=normalized_status,
                        message=safe_message,
                    ),
                )
            except Exception:
                self._attachment_states[safe_id] = current
                raise
            return True

    async def remove_attachment(self, attachment_id: str) -> bool:
        safe_id = _attachment_id(attachment_id)
        async with self._attachment_lock:
            current = self._attachment_states.pop(safe_id, None)
            if current is None:
                return False
            try:
                await self._bridge.send(
                    "attachment.remove",
                    AttachmentRemove(id=safe_id),
                )
            except Exception:
                self._attachment_states[safe_id] = current
                raise
            return True

    async def clear_attachments(self, *, status: str | None = None) -> int:
        """Clear every chip, or just chips in one terminal state."""

        normalized_status = _attachment_status(status) if status is not None else None
        async with self._attachment_lock:
            removed = {
                key: value
                for key, value in self._attachment_states.items()
                if normalized_status is None or value.status == normalized_status
            }
            if not removed:
                return 0
            for key in removed:
                self._attachment_states.pop(key, None)
            try:
                await self._bridge.send(
                    "attachment.clear",
                    AttachmentClear(status=normalized_status),
                )
            except Exception:
                self._attachment_states.update(removed)
                raise
            return len(removed)

    async def request_approval(
        self,
        request: dict[str, object],
        *,
        timeout: float | None = None,
    ) -> HostApprovalResponse | ExternalApprovalResolution | None:
        """Show the host approval overlay and await the user's decision.

        Returns None — which callers must treat as a deny — on timeout, on a
        bridge send failure, or when the surface shuts down while waiting.
        """
        approval_id = str(request.get("id") or "")
        if not approval_id:
            return None
        resolved = self._resolved_gateway_approvals.get(approval_id)
        if resolved is not None:
            return resolved
        wait_seconds = _APPROVAL_RESPONSE_TIMEOUT_SECONDS if timeout is None else timeout
        future = self._approval_waiters.get(approval_id)
        created = future is None
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._approval_waiters[approval_id] = future
        assert future is not None
        payload = _sanitized_approval_payload(request)
        try:
            # A push-first overlay may be a deliberately small preview. The
            # canonical request owns the choices, so refresh only when its
            # payload is actually different; identical re-entry stays quiet.
            if created or self._approval_requests.get(approval_id) != payload:
                await self._bridge.send("approval.request", payload)
                self._approval_requests[approval_id] = payload
            response = await asyncio.wait_for(asyncio.shield(future), wait_seconds)
            # A gateway resolved push can race a keypress that completed the
            # local future. If the push landed before this waiter resumed, the
            # gateway decision is authoritative and suppresses a stale second
            # resolve RPC from this client.
            return self._resolved_gateway_approvals.get(approval_id) or response
        except TimeoutError:
            log.warning("opentui.approval.timeout", approval_id=approval_id)
            if not future.done():
                future.set_result(None)
            await self._dismiss_host_approval(approval_id)
            return None
        except asyncio.CancelledError:
            # The turn stopped waiting (Ctrl+C, task teardown); close the host
            # overlay too, or the stale modal swallows the next keypress.
            if not future.done():
                future.set_result(None)
            if self._approval_waiters.get(approval_id) is future:
                self._approval_waiters.pop(approval_id, None)
            await self._dismiss_host_approval(approval_id)
            raise
        except Exception as exc:
            log.warning(
                "opentui.approval.request_failed",
                approval_id=approval_id,
                error=str(exc),
            )
            if not future.done():
                future.set_result(None)
            if self._approval_waiters.get(approval_id) is future:
                self._approval_waiters.pop(approval_id, None)
            self._approval_requests.pop(approval_id, None)
            await self._dismiss_host_approval(approval_id)
            return None
        finally:
            if future.done() and self._approval_waiters.get(approval_id) is future:
                self._approval_waiters.pop(approval_id, None)
            if future.done():
                self._approval_requests.pop(approval_id, None)

    async def present_gateway_approval(self, request: dict[str, object]) -> bool:
        """Pre-present a pushed approval without claiming its resolution RPC."""

        approval_id = str(request.get("id") or "")
        if not approval_id:
            return False
        if approval_id in self._resolved_gateway_approvals:
            return False
        # Push events are previews. They may arrive after the canonical tool
        # request and intentionally omit choices, so they must never replace
        # an overlay that already exists. ``request_approval`` is the only path
        # allowed to upgrade a preview with canonical details.
        if approval_id in self._approval_waiters:
            return True
        payload = _sanitized_approval_payload(request)
        future: asyncio.Future[HostApprovalResponse | ExternalApprovalResolution | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._approval_waiters[approval_id] = future
        try:
            await self._bridge.send("approval.request", payload)
            self._approval_requests[approval_id] = payload
        except Exception:
            self._approval_waiters.pop(approval_id, None)
            if not future.done():
                future.set_result(None)
            return False
        return True

    async def resolve_gateway_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        resolution: str | None = None,
    ) -> bool:
        """Apply a WebUI/other-client resolution and close the local overlay."""

        decision = ExternalApprovalResolution(
            id=approval_id,
            approved=approved,
            resolution=resolution,
        )
        self._resolved_gateway_approvals[approval_id] = decision
        while len(self._resolved_gateway_approvals) > 256:
            self._resolved_gateway_approvals.pop(next(iter(self._resolved_gateway_approvals)))
        future = self._approval_waiters.pop(approval_id, None)
        self._approval_requests.pop(approval_id, None)
        if future is not None and not future.done():
            future.set_result(decision)
        await self._dismiss_host_approval(approval_id)
        return future is not None

    async def _dismiss_host_approval(self, approval_id: str) -> None:
        """Best-effort: tell the host to close an overlay Python abandoned.

        A failed send is fine — the bridge is going down and taking the
        overlay with it.
        """
        try:
            await self._bridge.send("approval.dismiss", ApprovalDismiss(id=approval_id))
        except Exception as exc:
            log.debug(
                "opentui.approval.dismiss_failed",
                approval_id=approval_id,
                error=str(exc),
            )

    def deliver_approval_response(self, response: HostApprovalResponse) -> bool:
        """Resolve the waiter for one host approval decision, if still pending."""
        # Keep the completed waiter registered until the owning
        # ``request_approval`` coroutine reaches its ``finally`` block. A late
        # pushed preview can otherwise observe a transiently empty registry,
        # reopen the overlay, and leave an orphan waiter behind.
        future = self._approval_waiters.get(response.id)
        if future is None or future.done():
            log.warning("opentui.approval.unmatched_response", approval_id=response.id)
            return False
        future.set_result(response)
        return True

    def cancel_pending_approvals(self) -> None:
        """Unblock every pending approval waiter with a deny-safe None result."""
        while self._approval_waiters:
            _, future = self._approval_waiters.popitem()
            if not future.done():
                future.set_result(None)
        self._approval_requests.clear()

    async def fail_pending_gateway_approvals(self) -> None:
        """Fail closed and dismiss every overlay after gateway disconnect."""

        approval_ids = list(self._approval_waiters)
        self.cancel_pending_approvals()
        for approval_id in approval_ids:
            await self._dismiss_host_approval(approval_id)

    def stream_output(self) -> AbstractAsyncContextManager[Callable[[str], None]]:
        return _opentui_stream_output(self)

    def set_toolbar(self, key: str, value: object | None) -> None:
        if value is None:
            self._toolbar.pop(key, None)
            return
        self._toolbar[key] = value

    def invalidate(self) -> None:
        router_state = _router_plugin_state_from_toolbar(self._toolbar)
        _send_bridge_message(self._bridge, "router.update", router_state)


class OpenTuiSurface:
    """Adapter exposing the OpenTUI footer host through `TuiSurface`."""

    def __init__(
        self,
        bridge: _OpenTuiBridgeLike,
        *,
        approval_surface: Surface = Surface.CLI_GATEWAY,
        workspace_dir: Path | None = None,
    ) -> None:
        self._bridge = bridge
        self._workspace_dir = workspace_dir
        self._cancel_callback: Callable[[], None] | None = None
        self._shutdown_callback: Callable[[], None] | None = None
        self._eof_emitted = False
        self._consecutive_host_errors = 0
        self._completion_task: asyncio.Task[None] | None = None
        self._persist_tasks: set[asyncio.Task[None]] = set()
        self._last_resize: tuple[int, int] | None = None
        self._output_handle = OpenTuiOutputHandle(
            bridge,
            approval_surface=approval_surface,
        )

    async def next_line(self) -> TuiSubmittedInput | None:
        if self._eof_emitted:
            return None
        while True:
            message = await self._bridge.next_message()
            if message is None:
                self._cancel_pending_completion()
                self._output_handle.cancel_pending_approvals()
                return None
            if isinstance(message, HostError):
                # The host already recovered from whatever it is reporting;
                # treat the frame as a diagnostic and keep serving input. Only
                # an uninterrupted flood of error frames tears the session down.
                self._consecutive_host_errors += 1
                log.warning(
                    "opentui.host.error_frame",
                    message=message.message,
                    detail=message.detail,
                    consecutive=self._consecutive_host_errors,
                )
                if self._consecutive_host_errors >= _MAX_CONSECUTIVE_HOST_ERROR_FRAMES:
                    raise RuntimeError(f"OpenTUI host error: {message.message}")
                continue
            self._consecutive_host_errors = 0
            if isinstance(message, HostInputSubmit):
                return TuiSubmittedInput(
                    text=message.text,
                    intent=message.intent,
                    client_message_id=message.client_message_id or uuid.uuid4().hex,
                )
            if isinstance(message, HostApprovalResponse):
                # An overlay decision resolves its waiter and the loop keeps
                # pumping — it is never surfaced as chat input.
                self._output_handle.deliver_approval_response(message)
                continue
            if isinstance(message, HostCompletionRequest):
                self._start_completion(message)
                continue
            if isinstance(message, HostInputCancel):
                if self._cancel_callback is not None:
                    self._cancel_callback()
                continue
            if isinstance(message, HostInputEof):
                self._eof_emitted = True
                self._cancel_pending_completion()
                self._output_handle.cancel_pending_approvals()
                if self._shutdown_callback is not None:
                    self._shutdown_callback()
                return None
            if isinstance(message, HostProtocolUnknown):
                log.warning(
                    "opentui.host.protocol_unknown",
                    message_type=message.message_type,
                )
                continue
            if isinstance(message, HostThemeSelected):
                # A picker confirmation happens entirely in the host; persist
                # it here so the choice survives restarts like /theme <name>.
                # Fire-and-forget: this loop IS the input pump — awaiting the
                # write would queue the next input/cancel/approval frame
                # behind prefs IO. The save is best-effort by contract, so a
                # failure is logged by the prefs module, never raised here.
                from opensquilla.cli.tui.opentui.prefs import (  # noqa: PLC0415
                    save_theme_preference,
                )

                task = asyncio.get_running_loop().create_task(
                    asyncio.to_thread(save_theme_preference, message.name)
                )
                self._persist_tasks.add(task)
                task.add_done_callback(self._persist_tasks.discard)
                continue
            if isinstance(message, HostResize):
                self._last_resize = (message.width, message.height)
                continue
            if isinstance(message, HostReady):
                continue

    @property
    def last_known_size(self) -> tuple[int, int] | None:
        """Latest (width, height) the host reported, or None before any resize."""
        return self._last_resize

    @property
    def output_handle(self) -> OpenTuiOutputHandle:
        return self._output_handle

    @property
    def redraw_callback(self) -> Callable[[], None]:
        return self._output_handle.invalidate

    def set_cancel_callback(self, cb: Callable[[], None] | None) -> None:
        self._cancel_callback = cb

    def set_shutdown_callback(self, cb: Callable[[], None] | None) -> None:
        self._shutdown_callback = cb

    def emit_eof(self) -> None:
        self._eof_emitted = True
        self._output_handle.cancel_pending_approvals()

    async def write_through(self, payload: str) -> None:
        await self._output_handle.write_through(payload)

    async def send_message(self, message_type: str, payload: dict[str, object]) -> None:
        await self._output_handle.send_message(message_type, payload)

    def _start_completion(self, message: HostCompletionRequest) -> None:
        # Serve completions off the input loop so a queued input.submit is never
        # delayed behind workspace enumeration. Only the newest request matters:
        # supersede (cancel) any older one still running — the host drops stale
        # responses anyway. The attribute keeps a strong reference to the task.
        self._cancel_pending_completion()
        self._completion_task = asyncio.create_task(self._run_completion(message))

    def _cancel_pending_completion(self) -> None:
        task = self._completion_task
        if task is not None and not task.done():
            task.cancel()

    async def _run_completion(self, message: HostCompletionRequest) -> None:
        try:
            await self._handle_completion(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A failed (or unsendable) completion response must never take the
            # input loop down with it; the host simply keeps its local matches.
            log.warning("opentui.completion.failed", error=str(exc))

    async def _handle_completion(self, message: HostCompletionRequest) -> None:
        if message.kind != "file":
            await self._bridge.send(
                "completion.response",
                {"request_id": message.request_id, "kind": message.kind, "items": []},
            )
            return

        workspace_dir = self._workspace_dir
        if workspace_dir is None:
            await self._bridge.send(
                "completion.response",
                {"request_id": message.request_id, "kind": "file", "items": []},
            )
            return

        loop = asyncio.get_running_loop()
        paths = await loop.run_in_executor(
            None,
            lambda: enumerate_workspace_files(
                workspace_dir,
                query=message.query,
                max_results=50,
            ),
        )
        await self._bridge.send(
            "completion.response",
            {
                "request_id": message.request_id,
                "kind": "file",
                "items": [_file_completion_item(path) for path in paths],
            },
        )


@asynccontextmanager
async def _opentui_stream_output(
    output: OpenTuiOutputHandle,
) -> AsyncIterator[Callable[[str], None]]:
    pending: set[asyncio.Task[None]] = set()

    def _write(delta: str) -> None:
        if not delta:
            return
        # Prune finished writes on every call so a long stream never accumulates
        # completed tasks, and a mid-stream bridge failure surfaces on the next
        # delta instead of only at context exit.
        for task in [candidate for candidate in pending if candidate.done()]:
            pending.discard(task)
            if task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                raise error
        pending.add(asyncio.create_task(output.write_through(delta)))

    try:
        yield _write
    finally:
        if pending:
            await asyncio.gather(*pending)


@asynccontextmanager
async def open_opentui_surface(
    *,
    surface: Surface,
    model: str | None = None,
    session_id: str | None = None,
    ready_marker: str | None = None,
    print_ready_marker: bool = True,
    bridge: OpenTuiBridge | None = None,
    completion_context: CompletionContext | None = None,
    workspace_dir: Path | str | None = None,
    workspace_label: str | None = None,
    history_replace: HistoryReplace | None = None,
    context_update: ContextUpdate | None = None,
) -> AsyncIterator[TuiSurface]:
    active_bridge = bridge or OpenTuiBridge()
    active_workspace_dir = _normalize_workspace_dir(workspace_dir) or _workspace_dir()
    await active_bridge.start()
    try:
        # The readiness sentinel is harness scaffolding (tmux drivers wait for
        # it on screen), so it defaults OFF: it renders only when the caller or
        # the OPENSQUILLA_TUI_READY_MARKER env var explicitly provides one.
        marker = (
            os.environ.get("OPENSQUILLA_TUI_READY_MARKER", "")
            if ready_marker is None
            else ready_marker
        )
        if history_replace is not None:
            await active_bridge.send(
                "composer.set",
                ComposerState(placeholder="loading session history", disabled=True),
            )
            await active_bridge.send("history.replace", history_replace)
        await active_bridge.send(
            "composer.set",
            ComposerState(placeholder="send a message", disabled=False),
        )
        mode = "gateway" if surface is Surface.CLI_GATEWAY else "standalone"
        await active_bridge.send(
            "router.update",
            RouterPluginState(
                model=model or "default",
                route=mode,
                saving="-",
                context="ready",
                style="dim",
                source=session_id or "new session",
                routing_applied=False,
                rollout_phase="observe",
            ),
        )
        await active_bridge.send(
            "context.update",
            context_update
            if context_update is not None
            else context_update_from_bootstrap(
                None,
                surface=surface,
                model=model,
                session_id=session_id,
                workspace_label=(
                    workspace_label
                    or (str(active_workspace_dir) if active_workspace_dir is not None else None)
                ),
            ),
        )
        await active_bridge.send(
            "completion.context",
            completion_context
            if completion_context is not None
            else build_completion_context(surface, workspace_dir=active_workspace_dir),
        )
        if print_ready_marker and marker:
            await active_bridge.send("scrollback.write", ScrollbackWrite(text=f"{marker}\n"))
        yield OpenTuiSurface(
            active_bridge,
            approval_surface=surface,
            workspace_dir=active_workspace_dir,
        )
    finally:
        await active_bridge.close()


def _workspace_dir() -> Path | None:
    workspace = os.environ.get("OPENSQUILLA_WORKSPACE_DIR")
    if not workspace:
        return None
    return Path(workspace)


def _normalize_workspace_dir(workspace_dir: Path | str | None) -> Path | None:
    if workspace_dir is None:
        return None
    return Path(workspace_dir)


def _file_completion_item(path: str) -> dict[str, str]:
    # @path is currently a textual workspace reference carried inside
    # input.submit, not a staged attachment. It deliberately does not create a
    # chip until the host→Python input contract can carry structured references;
    # showing a ready chip today would falsely imply bytes were attached.
    return {
        "label": path,
        "description": path,
        "insert_text": f"@{path} ",
        "category": "file",
    }


def _attachment_status(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in _ATTACHMENT_STATUSES:
        raise ValueError("attachment status must be reading, uploading, ready, or failed")
    return normalized


def _attachment_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", str(value or "")).strip("-")
    if not normalized:
        raise ValueError("attachment id must not be empty")
    return normalized[:96]


def _attachment_kind(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").lower()).strip("-")
    return (normalized or "file")[:24]


def _attachment_label(value: str) -> str:
    # Normalize both POSIX and Windows separators before taking the basename;
    # the host must never receive an absolute local path.
    basename = str(value or "attachment").replace("\\", "/").rsplit("/", 1)[-1]
    clean = re.sub(r"[\x00-\x1f\x7f]+", "", basename).strip()
    return (clean or "attachment")[:72]


def _attachment_message(value: str) -> str:
    clean = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    # Defensive redaction for future callers: messages are UI diagnostics, not
    # transport payloads, so paths and byte counts never belong on the wire.
    clean = re.sub(r"(?i)(?:[a-z]:[\\/]|/)[^\s]+", "<path>", clean)
    clean = re.sub(r"(?i)\b\d[\d,._]*\s*bytes?\b", "<size>", clean)
    return " ".join(clean.split())[:120]


def _send_bridge_message(
    bridge: _OpenTuiBridgeLike,
    message_type: str,
    payload: object,
) -> None:
    send_nowait = getattr(bridge, "send_nowait", None)
    if callable(send_nowait):
        send_nowait(message_type, payload)
        return

    async def _send() -> None:
        await bridge.send(message_type, payload)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_send())
        return
    loop.create_task(_send())


def _router_plugin_state_from_toolbar(toolbar: dict[str, object]) -> RouterPluginState:
    label = str(toolbar.get("router_hud") or "")
    style = str(toolbar.get("router_hud_style") or "dim")
    context = _router_context_from_toolbar(toolbar)
    io = _router_io_from_toolbar(toolbar)
    baseline_model = str(toolbar.get("router_baseline_model") or "")
    source = str(toolbar.get("router_source") or "")
    routing_applied = bool(toolbar.get("router_routing_applied", True))
    rollout_phase = str(toolbar.get("router_rollout_phase") or "full")
    match = _ROUTER_LABEL_RE.match(label)
    if match:
        tier = match.group("tier")
        confidence = match.group("confidence")
        return RouterPluginState(
            model=match.group("model"),
            route=f"{tier} {confidence}" if confidence else tier,
            saving=match.group("saving") or "-",
            context=context,
            style=_normalize_router_style(style),
            baseline_model=baseline_model,
            source=source,
            routing_applied=routing_applied,
            rollout_phase=rollout_phase,
            io=io,
        )

    fallback = _FALLBACK_LABEL_RE.match(label)
    if fallback:
        return RouterPluginState(
            model=fallback.group("model"),
            route="fallback",
            saving="-",
            context=context,
            style="warning",
            baseline_model=baseline_model,
            source=source or "fallback",
            routing_applied=routing_applied,
            rollout_phase=rollout_phase,
            io=io,
        )

    return RouterPluginState(
        model="pending",
        route="pending",
        saving="-",
        context=context,
        style="dim",
        io=io,
    )


def _router_context_from_toolbar(toolbar: dict[str, object]) -> str:
    """Context PRESSURE only ("12%"), or "-" when the window is unknown.

    The last turn's in/out token pair travels separately (see
    ``_router_io_from_toolbar``): packing it in here made the field read like
    "34.6k used of a 548-token window" whenever the percent was unavailable.
    """
    session_input = _coerce_nonnegative_int(toolbar.get("router_session_input"))
    context_window = _coerce_positive_int(toolbar.get("router_context_window"))
    if session_input is None or context_window is None:
        return "-"

    pressure = min(max(session_input / context_window, 0.0), 1.0)
    percent = int(pressure * 100 + 0.5)
    return f"{percent}%"


def _router_io_from_toolbar(toolbar: dict[str, object]) -> str:
    """Last turn's token traffic ("34.6k/548" = in/out), or "" before any turn."""
    usage = toolbar.get("router_usage")
    return str(usage) if usage else ""


def _coerce_nonnegative_int(value: object | None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, int | float | str | bytes | bytearray):
        return None
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None


def _coerce_positive_int(value: object | None) -> int | None:
    coerced = _coerce_nonnegative_int(value)
    if coerced is None or coerced <= 0:
        return None
    return coerced


def _normalize_router_style(style: str) -> str:
    if style in {"dim", "normal", "warning", "error"}:
        return style
    return "normal"
