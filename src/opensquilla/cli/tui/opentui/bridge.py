"""Python side helpers for the OpenTUI footer host."""

from __future__ import annotations

import asyncio
import os
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from opensquilla import __version__
from opensquilla.cli.tui.backend.transcript import ViewportProjection
from opensquilla.cli.tui.opentui.host_runtime import (
    HostArtifact,
    HostArtifactResolver,
    HostFailureReason,
    HostProcessController,
    HostRuntimeError,
    source_host_requested,
)
from opensquilla.cli.tui.opentui.messages import (
    OPENTUI_SCREEN_MODE,
    HostError,
    HostReady,
    HostToPythonMessage,
    HostToPythonMessageError,
    ScrollbackWrite,
    host_message_from_json,
    python_message_to_json,
)
from opensquilla.cli.tui.opentui.prefs import load_theme_preference
from opensquilla.cli.tui.opentui.terminal import create_terminal_guardian
from opensquilla.cli.tui.opentui.themes import THEME_ENV_VAR
from opensquilla.cli.tui.opentui.transport import HostConnection
from opensquilla.cli.tui.renderers.selection import (
    RendererBackendAvailability,
    RendererBackendUnavailableReason,
)

DEFAULT_HOST_PACKAGE_DIR = Path(__file__).resolve().parent / "package"
DEFAULT_READY_TIMEOUT_SECONDS = 5.0
# Tolerate a burst of unparseable host lines (skip them) before giving up, so a
# stray corrupted line never tears down the UI but a wedged sidecar still does.
_MAX_CONSECUTIVE_MALFORMED_LINES = 64
# Frames queued for the host before the bridge gives up. A healthy host drains
# far faster than Python produces, so hitting this bound means the host stopped
# reading; erroring beats growing the queue without limit.
_WRITE_QUEUE_MAX_FRAMES = 8192
# These capabilities are semantic product contracts, not decorative handshake
# metadata. An older host may render basic text but cannot safely preserve turn
# identity/scroll anchors or expose the shared routing controls. Refuse that
# mixed-version surface explicitly instead of silently presenting a partially
# writable TUI.
REQUIRED_INTERACTIVE_HOST_CAPABILITIES = frozenset(
    {
        "turn.identity.v2",
        "scroll.anchor.v1",
        "model.routing.control.v1",
    }
)
log = structlog.get_logger(__name__)


class OpenTuiBridgeError(HostRuntimeError):
    """Raised when the OpenTUI host process cannot be used."""

    def __init__(
        self,
        message: str,
        *,
        reason: HostFailureReason = HostFailureReason.TRANSPORT,
    ) -> None:
        super().__init__(message, reason=reason)


@dataclass(frozen=True)
class OpenTuiHostPaths:
    package_dir: Path = DEFAULT_HOST_PACKAGE_DIR
    main_script: Path = DEFAULT_HOST_PACKAGE_DIR / "src" / "main.mjs"


def _host_handshake_mismatches(
    artifact: HostArtifact,
    message: HostReady,
) -> list[str]:
    mismatches: list[str] = []
    if message.protocol != artifact.protocol_version:
        mismatches.append(
            f"protocol expected={artifact.protocol_version!r} got={message.protocol!r}"
        )
    for label, expected, actual in (
        ("product", artifact.product_version, message.product_version),
        ("host", artifact.host_version, message.host_version),
        ("platform", artifact.platform, message.platform),
        ("arch", artifact.arch, message.arch),
        ("build", artifact.build_id, message.build_id),
        ("screen", OPENTUI_SCREEN_MODE, message.screen_mode),
    ):
        if actual != expected:
            mismatches.append(f"{label} expected={expected!r} got={actual!r}")
    missing_capabilities = sorted(
        REQUIRED_INTERACTIVE_HOST_CAPABILITIES.difference(message.capabilities)
    )
    if missing_capabilities:
        mismatches.append(
            "required interactive capabilities missing=" + ",".join(missing_capabilities)
        )
    return mismatches


def apply_theme_preference_env(env: dict[str, str]) -> None:
    """Fill ``OPENSQUILLA_TUI_THEME`` from the persisted /theme choice.

    A NON-EMPTY explicit environment value wins, preserving the documented
    OPENSQUILLA_TUI_THEME contract. An empty exported value counts as unset —
    the host maps it to the default theme anyway, so letting it mask the
    preference would only make /theme appear to save and then never apply.
    """
    if env.get(THEME_ENV_VAR):
        return
    saved_theme = load_theme_preference()
    if saved_theme:
        env[THEME_ENV_VAR] = saved_theme


def check_opentui_host_available(
    *,
    package_dir: Path = DEFAULT_HOST_PACKAGE_DIR,
    runtime_bin: str | None = None,
    use_source_host: bool | None = None,
    companion_module: Any | None = None,
) -> RendererBackendAvailability:
    """Check whether an exact companion or explicit source host can launch."""
    paths = OpenTuiHostPaths(package_dir=package_dir)
    resolver = HostArtifactResolver(
        package_dir=paths.package_dir,
        main_script=paths.main_script,
        runtime_bin=runtime_bin,
        use_source_host=(source_host_requested() if use_source_host is None else use_source_host),
        companion_module=companion_module,
    )
    try:
        resolver.resolve()
    except HostRuntimeError as exc:
        try:
            reason_code = RendererBackendUnavailableReason(exc.reason.value)
        except ValueError:
            # Keep a future host-runtime failure additive: an older core may
            # still explain the failure without crashing its auto fallback.
            reason_code = RendererBackendUnavailableReason.UNKNOWN
        return RendererBackendAvailability(
            available=False,
            reason=str(exc),
            reason_code=reason_code,
        )
    return RendererBackendAvailability(available=True)


class OpenTuiBridge:
    """Authenticated loopback JSON-line bridge to the OpenTUI footer host."""

    def __init__(
        self,
        *,
        runtime_bin: str | None = None,
        package_dir: Path = DEFAULT_HOST_PACKAGE_DIR,
        env: Mapping[str, str] | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        connection: HostConnection | None = None,
        process_controller: HostProcessController | None = None,
        artifact_resolver: HostArtifactResolver | None = None,
        use_source_host: bool | None = None,
    ) -> None:
        # Supplying a runtime is an explicit source-host developer override.
        # Normal installs leave this unset and resolve the packaged companion.
        self.runtime_bin = runtime_bin
        self.paths = OpenTuiHostPaths(package_dir=package_dir)
        self.env = dict(env or {})
        self.ready_timeout = ready_timeout
        self._connection = connection
        self._process_controller = process_controller or HostProcessController()
        self._artifact_resolver = artifact_resolver
        self._use_source_host = use_source_host
        self._terminal_guardian = create_terminal_guardian()
        self._host_artifact: HostArtifact | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_lines: deque[str] = deque(maxlen=50)
        self._stderr_task: asyncio.Task[None] | None = None
        self._closing = False
        self._write_queue: asyncio.Queue[str | None] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._write_error: OpenTuiBridgeError | None = None

    async def start(self) -> None:
        resolver = self._artifact_resolver or HostArtifactResolver(
            package_dir=self.paths.package_dir,
            main_script=self.paths.main_script,
            runtime_bin=self.runtime_bin,
            use_source_host=(
                source_host_requested() if self._use_source_host is None else self._use_source_host
            ),
        )
        try:
            artifact = resolver.resolve()
        except HostRuntimeError as exc:
            raise OpenTuiBridgeError(str(exc), reason=exc.reason) from exc
        self._host_artifact = artifact

        connection = self._connection or HostConnection(auth_timeout=self.ready_timeout)
        self._connection = connection
        try:
            await connection.listen()
        except HostRuntimeError as exc:
            raise OpenTuiBridgeError(str(exc), reason=exc.reason) from exc

        env = os.environ.copy()
        env.update(self.env)
        apply_theme_preference_env(env)
        env.update(connection.environment)
        env["OPENSQUILLA_PRODUCT_VERSION"] = __version__
        env["OPENSQUILLA_OPENTUI_HOST_VERSION"] = artifact.host_version
        env["OPENSQUILLA_OPENTUI_BUILD_ID"] = artifact.build_id
        env["OPENSQUILLA_OPENTUI_HOST_PLATFORM"] = artifact.platform
        env["OPENSQUILLA_OPENTUI_HOST_ARCH"] = artifact.arch

        # The host owns the shared tty (raw mode, alternate screen, mouse
        # tracking). Snapshot termios now so an abnormal host death can restore
        # a usable shell.
        self._save_terminal_state()

        try:
            self._process = await self._process_controller.spawn(artifact, env=env)
        except HostRuntimeError as exc:
            await connection.close()
            self._connection = None
            raise OpenTuiBridgeError(str(exc), reason=exc.reason) from exc

        # Capture stderr immediately so a process that fails before opening the
        # socket still yields an actionable crash reason.
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._log_host_version(artifact)
        try:
            await self._wait_for_host_connection(connection)
        except BaseException:
            await self.close()
            raise

        # All frames go through a single queue-draining writer task: send stays
        # non-blocking on the event loop even when the host stops reading and
        # the socket fills, and the one queue preserves global frame order.
        self._write_queue = asyncio.Queue(maxsize=_WRITE_QUEUE_MAX_FRAMES)
        self._writer_task = asyncio.create_task(self._drain_writes())

        try:
            message = await asyncio.wait_for(self.next_message(), timeout=self.ready_timeout)
        except TimeoutError:
            detail = await self._stderr_tail()
            await self.close()
            reason = f"OpenTUI host did not become ready within {self.ready_timeout:.1f}s"
            raise OpenTuiBridgeError(
                f"{reason} ({detail})" if detail else reason,
                reason=HostFailureReason.READY_TIMEOUT,
            ) from None
        except BaseException:
            # next_message already surfaces a crash reason (incl. captured stderr);
            # make sure we never leak the child process or stderr drain task.
            await self.close()
            raise
        if isinstance(message, HostReady):
            mismatches = _host_handshake_mismatches(artifact, message)
            if mismatches:
                await self.close()
                raise OpenTuiBridgeError(
                    "OpenTUI host handshake mismatch: " + "; ".join(mismatches),
                    reason=HostFailureReason.VERSION_MISMATCH,
                )
            return
        await self.close()
        if isinstance(message, HostError):
            raise OpenTuiBridgeError(message.message, reason=HostFailureReason.TRANSPORT)
        raise OpenTuiBridgeError(
            f"OpenTUI host did not become ready: {message!r}",
            reason=HostFailureReason.TRANSPORT,
        )

    async def _wait_for_host_connection(self, connection: HostConnection) -> None:
        """Wait for either an authenticated socket or an early process exit."""
        process = self._process
        if process is None:
            raise OpenTuiBridgeError(
                "OpenTUI host process is not started",
                reason=HostFailureReason.SPAWN,
            )
        connect_task = asyncio.create_task(connection.wait_for_client(timeout=self.ready_timeout))
        process_task = asyncio.create_task(process.wait())
        done, _pending = await asyncio.wait(
            {connect_task, process_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if connect_task in done:
            process_task.cancel()
            with suppress(asyncio.CancelledError):
                await process_task
            try:
                await connect_task
            except HostRuntimeError as exc:
                raise OpenTuiBridgeError(str(exc), reason=exc.reason) from exc
            return
        connect_task.cancel()
        with suppress(asyncio.CancelledError):
            await connect_task
        await process_task
        detail = await self._stderr_tail()
        returncode = process.returncode
        message = f"OpenTUI host exited with code {returncode}"
        if detail:
            message = f"{message}: {detail}"
        raise OpenTuiBridgeError(message, reason=HostFailureReason.RUNTIME_CRASH)

    def _log_host_version(self, artifact: HostArtifact) -> None:
        script = artifact.main_script or Path(artifact.command[0])
        try:
            mtime = script.stat().st_mtime
            mtime_iso = datetime.fromtimestamp(mtime, tz=UTC).isoformat()
        except OSError:
            mtime_iso = "unknown"
        pid = self._process.pid if self._process is not None else None
        log.info(
            "opentui.host.spawned",
            main_script=str(script),
            main_script_mtime=mtime_iso,
            host_pid=pid,
            host_source=artifact.source,
            host_version=artifact.host_version,
            host_build_id=artifact.build_id,
            screen_mode=OPENTUI_SCREEN_MODE,
        )

    async def send(self, message_type: str, payload: object | None = None) -> None:
        self.send_nowait(message_type, payload)

    def send_nowait(self, message_type: str, payload: object | None = None) -> None:
        if self._connection is None:
            raise OpenTuiBridgeError(
                "OpenTUI bridge is not started",
                reason=HostFailureReason.TRANSPORT,
            )
        if self._write_error is not None:
            raise OpenTuiBridgeError(
                "OpenTUI host IPC write failed",
                reason=HostFailureReason.TRANSPORT,
            ) from self._write_error
        frame = python_message_to_json(message_type, payload)
        queue = self._write_queue
        writer = self._writer_task
        if queue is None or writer is None or writer.done():
            raise OpenTuiBridgeError(
                "OpenTUI bridge writer is not running",
                reason=HostFailureReason.TRANSPORT,
            )
        # Enqueueing synchronously (no await point) keeps frame order exactly
        # equal to call order, even for fire-and-forget sender tasks.
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            raise OpenTuiBridgeError(
                "OpenTUI host stopped reading IPC frames (write queue overflow)",
                reason=HostFailureReason.TRANSPORT,
            ) from None

    async def _drain_writes(self) -> None:
        """Writer task: drain queued frames to the host off the event loop."""
        queue = self._write_queue
        if queue is None:
            return
        while True:
            frame = await queue.get()
            try:
                if frame is None:
                    return
                try:
                    connection = self._connection
                    if connection is None:
                        raise OpenTuiBridgeError(
                            "OpenTUI bridge is not started",
                            reason=HostFailureReason.TRANSPORT,
                        )
                    await connection.send_frame(frame)
                except (OpenTuiBridgeError, HostRuntimeError) as exc:
                    # Remember the failure so the next send raises it; frames
                    # still queued are undeliverable and dropped with the socket.
                    self._write_error = OpenTuiBridgeError(
                        str(exc), reason=HostFailureReason.TRANSPORT
                    )
                    return
            finally:
                queue.task_done()

    async def _flush_writes(self, timeout: float) -> None:
        """Ask the writer to drain everything queued so far, then stop."""
        queue = self._write_queue
        task = self._writer_task
        if queue is None or task is None or task.done():
            return
        with suppress(asyncio.QueueFull):
            queue.put_nowait(None)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    async def next_message(self) -> HostToPythonMessage | None:
        connection = self._connection
        if connection is None:
            raise OpenTuiBridgeError(
                "OpenTUI bridge is not started",
                reason=HostFailureReason.TRANSPORT,
            )
        malformed = 0
        while True:
            try:
                line = await connection.readline()
            except HostRuntimeError as exc:
                raise OpenTuiBridgeError(str(exc), reason=exc.reason) from exc
            if line == "":
                await self._raise_if_host_crashed()
                return None
            if not line.strip():
                continue
            try:
                return host_message_from_json(line)
            except HostToPythonMessageError as exc:
                # A single corrupted/garbage line must not kill the session — skip
                # it. Only give up if the host floods unparseable output, which
                # signals a genuinely wedged sidecar.
                malformed += 1
                if malformed > _MAX_CONSECUTIVE_MALFORMED_LINES:
                    raise
                with suppress(Exception):
                    log.warning("opentui.host.malformed_line", error=str(exc))
                continue

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                raw = await process.stderr.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip("\n")
                if text:
                    self._stderr_lines.append(text)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive: never let drain crash
            return

    async def _stderr_tail(self) -> str:
        task = self._stderr_task
        if task is not None and not task.done():
            with suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        return " | ".join(self._stderr_lines)

    async def _raise_if_host_crashed(self) -> None:
        """Distinguish a host crash from a clean EOF when the read pipe closes."""
        if self._closing:
            return
        process = self._process
        if process is None:
            return
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=1.0)
        returncode = process.returncode
        if returncode is None or returncode == 0:
            return
        # The dead host never ran its own terminal teardown; reset the tty
        # before raising so the crash reason is readable in a sane shell.
        self._restore_terminal()
        detail = await self._stderr_tail()
        message = f"OpenTUI host exited with code {returncode}"
        if detail:
            message = f"{message}: {detail}"
        raise OpenTuiBridgeError(message, reason=HostFailureReason.RUNTIME_CRASH)

    async def close(self) -> None:
        self._closing = True
        process = self._process
        if self._connection is not None:
            with suppress(Exception):
                self.send_nowait("shutdown")
            # Deliver everything still queued (including the shutdown frame) so
            # a healthy host can exit on its own before any signal is sent.
            await self._flush_writes(timeout=1.0)
        if process is not None and process.returncode is None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=0.5)
        if process is not None and process.returncode is None:
            await self._process_controller.stop(process)
        writer_task = self._writer_task
        if writer_task is not None:
            if not writer_task.done():
                writer_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await writer_task
            self._writer_task = None
        self._write_queue = None
        connection = self._connection
        if connection is not None:
            await connection.close()
            self._connection = None
        stderr_task = self._stderr_task
        if stderr_task is not None:
            stderr_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await stderr_task
            self._stderr_task = None
        if process is not None and process.returncode not in (None, 0):
            # Nonzero/signal exit: the host may have died without restoring the
            # terminal it owned. A clean exit (0) already restored it.
            self._restore_terminal()
        self._process = None
        self._host_artifact = None

    def _save_terminal_state(self) -> None:
        self._terminal_guardian.capture()

    def _restore_terminal(self) -> None:
        """Best-effort tty reset after the host died without its own teardown."""
        self._terminal_guardian.restore()

    async def write_scrollback(self, payload: str) -> None:
        await self.send("scrollback.write", ScrollbackWrite(text=payload))


@dataclass
class OpenTuiReplayRenderer:
    """Headless renderer facade used for backend contract tests and evaluation."""

    buffer: str = ""
    reasoning_buffer: str = ""
    intermediate_buffer: str = ""
    flush_count: int = 0
    statuses: list[tuple[str, str]] = field(default_factory=list)
    tool_events: list[tuple[str, str | None]] = field(default_factory=list)

    async def aappend_text(self, delta: str, *, presentation: str = "answer") -> None:
        if presentation == "intermediate":
            self.intermediate_buffer += delta
        else:
            self.buffer += delta
        self.flush_count += 1

    async def areconcile_final_text(self, text: str) -> None:
        self.buffer = text
        self.intermediate_buffer = ""

    async def aappend_reasoning(self, delta: str) -> None:
        self.reasoning_buffer += delta

    async def atool_start(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        tool_use_id: str | None = None,
    ) -> None:
        del args
        self.tool_events.append((f"start:{name}", tool_use_id))

    async def atool_finished(
        self,
        tool_use_id: str | None,
        *,
        success: bool,
        elapsed: float | None = None,
        error: str | None = None,
        result: object | None = None,
    ) -> None:
        del elapsed, error, result
        self.tool_events.append(("done" if success else "error", tool_use_id))

    async def astatus(self, message: str, *, style: str = "dim") -> None:
        self.statuses.append((message, style))

    async def aerror(self, message: str) -> None:
        self.statuses.append((message, "error"))

    def pulse(self) -> None:
        return None

    async def afinalize(self, usage: Any | None = None, *, cancelled: bool = False) -> None:
        del usage
        if cancelled:
            self.statuses.append(("cancelled", "dim"))

    async def aclose(self) -> None:
        return None

    def render_structured_layout(
        self,
        *,
        plugin_snapshots: dict[str, object],
        transcript_projection: ViewportProjection,
    ) -> dict[str, int | tuple[str, ...]]:
        return {
            "plugin_slots": tuple(sorted(plugin_snapshots)),
            "visible_items": len(transcript_projection.items),
            "total_items": transcript_projection.total_items,
            "total_rows": transcript_projection.total_rows,
        }


@dataclass(frozen=True)
class OpenTuiRendererBackend:
    backend_id: str = "opentui"
    supports_structured_ui: bool = True
    supports_streaming_fast_path: bool = True

    def is_available(self) -> RendererBackendAvailability:
        return check_opentui_host_available()

    def create_renderer(self, **kwargs: Any) -> OpenTuiReplayRenderer:
        del kwargs
        return OpenTuiReplayRenderer()
