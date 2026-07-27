from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from collections import deque
from types import SimpleNamespace

import pytest

from opensquilla import __version__
from opensquilla.cli.tui import opentui as _opentui_pkg  # noqa: F401  (ensure package import)
from opensquilla.cli.tui.opentui import bridge as bridge_module
from opensquilla.cli.tui.opentui import host_runtime as host_runtime_module
from opensquilla.cli.tui.opentui.bridge import (
    OpenTuiBridge,
    OpenTuiBridgeError,
    OpenTuiHostPaths,
    check_opentui_host_available,
)
from opensquilla.cli.tui.opentui.host_runtime import (
    HOST_PROTOCOL_VERSION,
    HostArtifactResolver,
    HostFailureReason,
    HostRuntimeError,
)
from opensquilla.cli.tui.opentui.messages import (
    HostInputSubmit,
    HostReady,
    HostToPythonMessageError,
    ScrollbackWrite,
)
from opensquilla.cli.tui.opentui.terminal import (
    TERMINAL_RESET_SEQUENCE,
    PosixTerminalGuardian,
)
from opensquilla.cli.tui.renderers.selection import RendererBackendUnavailableReason


class _FakeConnection:
    def __init__(self, *lines: str) -> None:
        self.lines = deque(lines)
        self.sent: list[str] = []
        self.closed = False

    async def readline(self) -> str:
        return self.lines.popleft() if self.lines else ""

    async def send_frame(self, frame: str) -> None:
        self.sent.append(frame)

    async def close(self) -> None:
        self.closed = True


def _prepare_source_host(package_dir, main_script) -> None:
    (package_dir / "node_modules" / "@opentui" / "core").mkdir(parents=True)
    assert main_script.exists()


def _fake_companion(command: tuple[str, ...], **overrides: object) -> SimpleNamespace:
    metadata = {
        "product_version": __version__,
        "host_version": __version__,
        "protocol_version": HOST_PROTOCOL_VERSION,
        "platform": host_runtime_module._current_platform(),
        "arch": host_runtime_module._current_arch(),
        "build_id": "unit-companion",
    }
    metadata.update(overrides)
    value = SimpleNamespace(**metadata)
    return SimpleNamespace(
        host_metadata=lambda: value,
        host_command=lambda: command,
    )


def test_missing_opentui_host_dependencies_report_install_command(tmp_path, monkeypatch) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    monkeypatch.setattr(host_runtime_module.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    availability = check_opentui_host_available(package_dir=package_dir, runtime_bin="bun")

    assert availability.available is False
    assert availability.reason is not None
    assert availability.reason_code is RendererBackendUnavailableReason.MISSING
    assert "@opentui/core" in availability.reason
    assert f"bun install --cwd {package_dir}" in availability.reason


def test_opentui_host_availability_is_not_blocked_on_windows(tmp_path, monkeypatch) -> None:
    package_dir = tmp_path / "package"
    (package_dir / "node_modules" / "@opentui" / "core").mkdir(parents=True)
    (package_dir / "src").mkdir()
    (package_dir / "src" / "main.mjs").write_text("", encoding="utf-8")
    monkeypatch.setattr(host_runtime_module.os, "name", "nt")
    monkeypatch.setattr(host_runtime_module.shutil, "which", lambda cmd: cmd)

    availability = check_opentui_host_available(package_dir=package_dir, runtime_bin="bun")

    assert availability.available is True
    assert availability.reason is None


def test_packaged_companion_is_available_without_bun(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(host_runtime_module.shutil, "which", lambda _cmd: None)

    availability = check_opentui_host_available(
        package_dir=tmp_path,
        companion_module=_fake_companion((str(tmp_path / "host"),)),
    )

    assert availability.available is True
    assert availability.reason is None


def test_missing_companion_does_not_advertise_an_unpublished_installer(
    tmp_path,
    monkeypatch,
) -> None:
    def missing_module(_name: str) -> None:
        raise ModuleNotFoundError("opensquilla_tui_host")

    monkeypatch.setattr(host_runtime_module.importlib, "import_module", missing_module)
    resolver = HostArtifactResolver(
        package_dir=tmp_path,
        main_script=tmp_path / "main.mjs",
    )

    with pytest.raises(HostRuntimeError) as exc_info:
        resolver.resolve()

    message = str(exc_info.value)
    assert exc_info.value.reason is HostFailureReason.MISSING
    assert "does not publish one" in message
    assert "--ui plain" in message
    assert "Reinstall OpenSquilla" not in message


def test_companion_version_mismatch_has_stable_failure_reason(tmp_path) -> None:
    resolver = HostArtifactResolver(
        package_dir=tmp_path,
        main_script=tmp_path / "main.mjs",
        companion_module=_fake_companion((str(tmp_path / "host"),), product_version="0.0.0-wrong"),
    )

    with pytest.raises(HostRuntimeError) as exc_info:
        resolver.resolve()

    assert exc_info.value.reason is HostFailureReason.VERSION_MISMATCH
    assert "version mismatch" in str(exc_info.value)


def test_host_handshake_rejects_missing_interactive_capabilities(tmp_path) -> None:
    artifact = HostArtifactResolver(
        package_dir=tmp_path,
        main_script=tmp_path / "main.mjs",
        companion_module=_fake_companion((sys.executable,)),
    ).resolve()
    ready = HostReady(
        protocol=artifact.protocol_version,
        product_version=artifact.product_version,
        host_version=artifact.host_version,
        platform=artifact.platform,
        arch=artifact.arch,
        build_id=artifact.build_id,
        capabilities=("jsonl", "turn.identity.v2"),
    )

    mismatches = bridge_module._host_handshake_mismatches(artifact, ready)

    assert mismatches == [
        "required interactive capabilities missing="
        "model.routing.control.v1,scroll.anchor.v1"
    ]


async def _attach_exited_process(bridge: OpenTuiBridge, *, code: int, stderr: str) -> None:
    """Attach a real, already-spawned child that exits with ``code`` to the bridge."""
    script = f"import sys; sys.stderr.write({stderr!r}); sys.exit({code})"
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, stderr=asyncio.subprocess.PIPE
    )
    bridge._process = process
    bridge._stderr_task = asyncio.create_task(bridge._drain_stderr())
    bridge._connection = _FakeConnection()


@pytest.mark.asyncio
async def test_next_message_raises_with_stderr_when_host_crashes() -> None:
    bridge = OpenTuiBridge()
    await _attach_exited_process(bridge, code=3, stderr="fatal: boom\n")

    with pytest.raises(OpenTuiBridgeError) as exc_info:
        await bridge.next_message()

    message = str(exc_info.value)
    assert "code 3" in message
    assert "fatal: boom" in message


@pytest.mark.asyncio
async def test_next_message_returns_none_on_clean_host_exit() -> None:
    bridge = OpenTuiBridge()
    await _attach_exited_process(bridge, code=0, stderr="")

    assert await bridge.next_message() is None


@pytest.mark.asyncio
async def test_next_message_tolerates_malformed_line_logging_failure(monkeypatch) -> None:
    """Diagnostic logging failures must not turn skipped garbage into a crash."""

    from opensquilla.cli.tui.opentui.messages import HostInputSubmit

    def raise_closed_file(*_args: object, **_kwargs: object) -> None:
        raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(bridge_module.log, "warning", raise_closed_file)

    bridge = OpenTuiBridge(
        connection=_FakeConnection(
            "plain text, not json\n",
            '{"type":"input.submit","text":"survived"}\n',
        )
    )

    message = await bridge.next_message()

    assert isinstance(message, HostInputSubmit)
    assert message.text == "survived"


@pytest.mark.asyncio
async def test_close_does_not_treat_intentional_shutdown_as_crash() -> None:
    bridge = OpenTuiBridge()
    await _attach_exited_process(bridge, code=7, stderr="ignored\n")

    # close() flips the closing guard, reaps the child, and cancels stderr draining
    # without raising even though the child exited non-zero.
    await bridge.close()

    assert bridge._stderr_task is None
    assert bridge._process is None


@pytest.mark.asyncio
async def test_start_surfaces_reason_and_cleans_up_when_host_crashes_on_launch(
    tmp_path,
) -> None:
    # A stand-in "host" that crashes immediately, exercising the real start()
    # handshake, socket plumbing, stderr capture, and crash detection without Bun.
    host_script = tmp_path / "fake_host.py"
    host_script.write_text(
        "import sys\nsys.stderr.write('startup boom\\n')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    _prepare_source_host(tmp_path, host_script)

    bridge = OpenTuiBridge(runtime_bin=sys.executable, package_dir=tmp_path, ready_timeout=5.0)
    bridge.paths = OpenTuiHostPaths(package_dir=tmp_path, main_script=host_script)

    with pytest.raises(OpenTuiBridgeError) as exc_info:
        await bridge.start()

    message = str(exc_info.value)
    assert "code 1" in message
    assert "startup boom" in message
    # start() must not leak the child process or stderr drain task on failure.
    assert bridge._process is None
    assert bridge._stderr_task is None


@pytest.mark.asyncio
async def test_start_uses_authenticated_loopback_and_reads_versioned_ready(
    tmp_path, monkeypatch
) -> None:
    host_script = tmp_path / "fake_host.py"
    host_script.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import socket

            sock = socket.create_connection((
                os.environ["OPENSQUILLA_OPENTUI_IPC_HOST"],
                int(os.environ["OPENSQUILLA_OPENTUI_IPC_PORT"]),
            ))
            stream = sock.makefile("rwb", buffering=0)
            auth = {
                "type": "auth",
                "token": os.environ["OPENSQUILLA_OPENTUI_IPC_TOKEN"],
                "protocol": int(os.environ["OPENSQUILLA_OPENTUI_PROTOCOL_VERSION"]),
            }
            stream.write((json.dumps(auth) + "\\n").encode())
            assert json.loads(stream.readline())["type"] == "auth.ok"
            ready = {
                "type": "ready",
                "protocol": 1,
                "productVersion": os.environ["OPENSQUILLA_PRODUCT_VERSION"],
                "hostVersion": os.environ["OPENSQUILLA_OPENTUI_HOST_VERSION"],
                "platform": os.environ["OPENSQUILLA_OPENTUI_HOST_PLATFORM"],
                "arch": os.environ["OPENSQUILLA_OPENTUI_HOST_ARCH"],
                "buildId": os.environ["OPENSQUILLA_OPENTUI_BUILD_ID"],
                "screenMode": "alternate-screen",
                "capabilities": [
                    "jsonl",
                    "loopback",
                    "authenticated",
                    "turn.identity.v2",
                    "scroll.anchor.v1",
                    "model.routing.control.v1",
                ],
            }
            stream.write((json.dumps(ready) + "\\n").encode())
            for line in stream:
                if json.loads(line).get("type") == "shutdown":
                    break
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(host_runtime_module.shutil, "which", lambda _cmd: None)
    resolver = HostArtifactResolver(
        package_dir=tmp_path,
        main_script=host_script,
        companion_module=_fake_companion((sys.executable, str(host_script))),
    )
    bridge = OpenTuiBridge(package_dir=tmp_path, artifact_resolver=resolver)

    await asyncio.wait_for(bridge.start(), timeout=5.0)
    await asyncio.wait_for(bridge.close(), timeout=5.0)

    assert bridge._process is None
    assert bridge._connection is None


@pytest.mark.asyncio
async def test_missing_host_has_stable_typed_failure() -> None:
    missing = SimpleNamespace(
        host_metadata=lambda: (_ for _ in ()).throw(RuntimeError("not installed")),
        host_command=lambda: (),
    )
    bridge = OpenTuiBridge(
        artifact_resolver=HostArtifactResolver(
            package_dir=bridge_module.DEFAULT_HOST_PACKAGE_DIR,
            main_script=bridge_module.DEFAULT_HOST_PACKAGE_DIR / "src/main.mjs",
            companion_module=missing,
        )
    )

    with pytest.raises(OpenTuiBridgeError) as exc_info:
        await bridge.start()

    assert exc_info.value.reason is HostFailureReason.MISSING


@pytest.mark.asyncio
async def test_next_message_gives_up_after_a_malformed_line_flood() -> None:
    """A wedged sidecar flooding garbage must escalate to a raise instead of
    spinning the read loop forever."""
    bridge = OpenTuiBridge(connection=_FakeConnection(*(["plain text, not json\n"] * 65)))

    with pytest.raises(HostToPythonMessageError):
        await bridge.next_message()


@pytest.mark.asyncio
async def test_next_message_delivers_after_exactly_the_flood_limit() -> None:
    """The escalation threshold is strict: 64 consecutive garbage lines are
    still skipped and the following valid message is delivered."""
    bridge = OpenTuiBridge(
        connection=_FakeConnection(
            *(["plain text, not json\n"] * 64),
            '{"type":"input.submit","text":"survived"}\n',
        )
    )

    message = await bridge.next_message()

    assert message == HostInputSubmit(text="survived")


@pytest.mark.asyncio
async def test_close_kills_wedged_host_instead_of_deadlocking(tmp_path) -> None:
    """A host that never connects and ignores terminate must still be killed."""
    host_script = tmp_path / "wedged_host.py"
    host_script.write_text(
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    _prepare_source_host(tmp_path, host_script)

    bridge = OpenTuiBridge(runtime_bin=sys.executable, package_dir=tmp_path, ready_timeout=0.5)
    bridge.paths = OpenTuiHostPaths(package_dir=tmp_path, main_script=host_script)

    with pytest.raises(OpenTuiBridgeError, match="did not become ready"):
        await asyncio.wait_for(bridge.start(), timeout=20.0)

    assert bridge._process is None
    assert bridge._connection is None


@pytest.mark.asyncio
async def test_send_nowait_serializes_lone_surrogates_without_raising() -> None:
    connection = _FakeConnection()
    bridge = OpenTuiBridge(connection=connection)
    bridge._write_queue = asyncio.Queue(maxsize=64)
    bridge._writer_task = asyncio.create_task(bridge._drain_writes())

    bridge.send_nowait("scrollback.write", ScrollbackWrite(text="file_\udc80.txt"))
    await bridge._flush_writes(timeout=1.0)

    assert len(connection.sent) == 1
    assert connection.sent[0].endswith("\n")
    assert "file_\udc80.txt" in connection.sent[0]


@pytest.mark.asyncio
async def test_writer_failure_is_reported_by_the_next_send() -> None:
    class _BrokenConnection(_FakeConnection):
        async def send_frame(self, frame: str) -> None:
            del frame
            raise HostRuntimeError("write failed", reason=HostFailureReason.TRANSPORT)

    bridge = OpenTuiBridge(connection=_BrokenConnection())
    bridge._write_queue = asyncio.Queue(maxsize=64)
    bridge._writer_task = asyncio.create_task(bridge._drain_writes())

    bridge.send_nowait("shutdown")
    await bridge._writer_task

    with pytest.raises(OpenTuiBridgeError, match="IPC write failed"):
        bridge.send_nowait("shutdown")


@pytest.mark.asyncio
async def test_start_reports_missing_bun_reason_instead_of_spawn_error(monkeypatch) -> None:
    monkeypatch.setattr(host_runtime_module.shutil, "which", lambda _cmd: None)

    bridge = OpenTuiBridge(use_source_host=True)

    assert bridge.runtime_bin is None
    with pytest.raises(OpenTuiBridgeError, match="Bun is not installed"):
        await bridge.start()


@pytest.mark.asyncio
async def test_start_reports_bogus_runtime_bin_with_actionable_reason(
    tmp_path, monkeypatch
) -> None:
    package_dir = tmp_path / "package"
    (package_dir / "node_modules" / "@opentui" / "core").mkdir(parents=True)
    (package_dir / "src").mkdir()
    (package_dir / "src" / "main.mjs").write_text("", encoding="utf-8")
    monkeypatch.setattr(host_runtime_module.os, "name", "posix")

    bridge = OpenTuiBridge(runtime_bin=str(tmp_path / "no-such-runtime"), package_dir=package_dir)

    with pytest.raises(OpenTuiBridgeError, match="not executable"):
        await bridge.start()


@pytest.mark.asyncio
async def test_start_wraps_vanished_runtime_as_bridge_error(tmp_path) -> None:
    """A runtime that disappears between the availability check and the spawn
    must still surface as a catchable OpenTuiBridgeError, not FileNotFoundError."""
    bridge = OpenTuiBridge(runtime_bin=str(tmp_path / "vanished-bin"), package_dir=tmp_path)

    with pytest.raises(OpenTuiBridgeError, match="not executable"):
        await bridge.start()


@pytest.mark.asyncio
async def test_writer_task_keeps_loop_responsive_and_preserves_frame_order() -> None:
    gate = asyncio.Event()

    class _StalledConnection(_FakeConnection):
        async def send_frame(self, frame: str) -> None:
            await gate.wait()
            self.sent.append(frame)

    connection = _StalledConnection()
    bridge = OpenTuiBridge(connection=connection)
    bridge._write_queue = asyncio.Queue(maxsize=64)
    bridge._writer_task = asyncio.create_task(bridge._drain_writes())

    for index in range(3):
        bridge.send_nowait("scrollback.write", ScrollbackWrite(text=f"frame-{index}"))
    await asyncio.sleep(0.05)
    assert connection.sent == []
    bridge.send_nowait("scrollback.write", ScrollbackWrite(text="frame-3"))

    gate.set()
    await bridge._flush_writes(timeout=5.0)

    texts = [json.loads(frame)["text"] for frame in connection.sent]
    assert texts == [f"frame-{index}" for index in range(4)]


@pytest.mark.asyncio
async def test_host_crash_triggers_terminal_restore(monkeypatch) -> None:
    bridge = OpenTuiBridge()
    restored: list[bool] = []
    monkeypatch.setattr(bridge, "_restore_terminal", lambda: restored.append(True))
    await _attach_exited_process(bridge, code=3, stderr="fatal: boom\n")

    with pytest.raises(OpenTuiBridgeError):
        await bridge.next_message()

    assert restored == [True]


@pytest.mark.asyncio
async def test_clean_host_exit_skips_terminal_restore(monkeypatch) -> None:
    bridge = OpenTuiBridge()
    restored: list[bool] = []
    monkeypatch.setattr(bridge, "_restore_terminal", lambda: restored.append(True))
    await _attach_exited_process(bridge, code=0, stderr="")

    assert await bridge.next_message() is None
    await bridge.close()

    assert restored == []


def test_restore_terminal_writes_reset_sequence_once() -> None:
    bridge = OpenTuiBridge()
    read_fd, write_fd = os.pipe()
    try:
        guardian = PosixTerminalGuardian()
        guardian.tty_fd = write_fd
        bridge._terminal_guardian = guardian
        bridge._restore_terminal()
        bridge._restore_terminal()
    finally:
        os.close(write_fd)
    with os.fdopen(read_fd, "rb") as reader:
        data = reader.read()

    assert data == TERMINAL_RESET_SEQUENCE
    assert b"\x1b[?1049l" in data
    assert b"\x1b[?25h" in data


@pytest.mark.asyncio
async def test_start_applies_persisted_theme_to_the_host_env(tmp_path, monkeypatch) -> None:
    # Binds the call site, not just the helper: a saved /theme preference must
    # reach the spawned host process as OPENSQUILLA_TUI_THEME.
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("OPENSQUILLA_TUI_THEME", raising=False)
    from opensquilla.cli.tui.opentui.prefs import save_theme_preference

    save_theme_preference("nord")

    seen_theme = tmp_path / "seen_theme.txt"
    host_script = tmp_path / "fake_host.py"
    host_script.write_text(
        textwrap.dedent(
            f"""
            import json
            import os
            import socket

            open({str(seen_theme)!r}, "w").write(
                os.environ.get("OPENSQUILLA_TUI_THEME", "<unset>")
            )
            sock = socket.create_connection((
                os.environ["OPENSQUILLA_OPENTUI_IPC_HOST"],
                int(os.environ["OPENSQUILLA_OPENTUI_IPC_PORT"]),
            ))
            stream = sock.makefile("rwb", buffering=0)
            auth = {{
                "type": "auth",
                "token": os.environ["OPENSQUILLA_OPENTUI_IPC_TOKEN"],
                "protocol": int(os.environ["OPENSQUILLA_OPENTUI_PROTOCOL_VERSION"]),
            }}
            stream.write((json.dumps(auth) + "\\n").encode())
            assert json.loads(stream.readline())["type"] == "auth.ok"
            ready = {{
                "type": "ready",
                "protocol": 1,
                "productVersion": os.environ["OPENSQUILLA_PRODUCT_VERSION"],
                "hostVersion": os.environ["OPENSQUILLA_OPENTUI_HOST_VERSION"],
                "platform": os.environ["OPENSQUILLA_OPENTUI_HOST_PLATFORM"],
                "arch": os.environ["OPENSQUILLA_OPENTUI_HOST_ARCH"],
                "buildId": os.environ["OPENSQUILLA_OPENTUI_BUILD_ID"],
                "screenMode": "alternate-screen",
                "capabilities": [
                    "jsonl",
                    "loopback",
                    "authenticated",
                    "turn.identity.v2",
                    "scroll.anchor.v1",
                    "model.routing.control.v1",
                ],
            }}
            stream.write((json.dumps(ready) + "\\n").encode())
            for line in stream:
                if json.loads(line).get("type") == "shutdown":
                    break
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(host_runtime_module.shutil, "which", lambda _cmd: None)
    resolver = HostArtifactResolver(
        package_dir=tmp_path,
        main_script=host_script,
        companion_module=_fake_companion((sys.executable, str(host_script))),
    )
    bridge = OpenTuiBridge(package_dir=tmp_path, artifact_resolver=resolver)

    await asyncio.wait_for(bridge.start(), timeout=5.0)
    await asyncio.wait_for(bridge.close(), timeout=5.0)

    assert seen_theme.read_text() == "nord"
