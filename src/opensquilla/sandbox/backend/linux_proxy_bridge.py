"""Linux managed-network bridge for bubblewrap sandboxes.

Bubblewrap's network namespace isolation makes the host-side managed proxy
unreachable from inside the sandbox. This module bridges that gap without
reopening host networking:

* :class:`LinuxProxyBridgeHost` listens on a host Unix-domain socket and
  forwards every accepted stream to the guarded local proxy.
* A standalone copy of this module is written beside that socket and run
  inside the sandbox. It listens on loopback TCP, connects each local client
  to the host Unix socket, and starts the caller's command with HTTP(S) proxy
  environment variables pointing at that inner loopback listener.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import suppress
from pathlib import Path

_CHUNK_SIZE = 64 * 1024
_INNER_PROXY_HOST = "127.0.0.1"

ENV_PROXY_UDS = "OPENSQUILLA_SANDBOX_PROXY_UDS"
ENV_PROXY_PORT = "OPENSQUILLA_SANDBOX_PROXY_PORT"
ENV_POLICY_B64 = "OPENSQUILLA_SANDBOX_POLICY_B64"
ENV_EXEC_WRAPPER = "OPENSQUILLA_SANDBOX_EXEC_WRAPPER"
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "YARN_HTTP_PROXY",
    "YARN_HTTPS_PROXY",
    "npm_config_http_proxy",
    "npm_config_https_proxy",
    "npm_config_proxy",
    "NPM_CONFIG_HTTP_PROXY",
    "NPM_CONFIG_HTTPS_PROXY",
    "NPM_CONFIG_PROXY",
    "BUNDLE_HTTP_PROXY",
    "BUNDLE_HTTPS_PROXY",
    "PIP_PROXY",
    "DOCKER_HTTP_PROXY",
    "DOCKER_HTTPS_PROXY",
    "WS_PROXY",
    "WSS_PROXY",
    "ws_proxy",
    "wss_proxy",
    "ALL_PROXY",
    "all_proxy",
    "FTP_PROXY",
    "ftp_proxy",
)
NO_PROXY_ENV_KEYS = (
    "NO_PROXY",
    "no_proxy",
    "npm_config_noproxy",
    "NPM_CONFIG_NOPROXY",
    "YARN_NO_PROXY",
    "BUNDLE_NO_PROXY",
)
PROXY_CONTROL_ENV = (
    ("CODEX_NETWORK_PROXY_ACTIVE", "1"),
    ("CODEX_NETWORK_ALLOW_LOCAL_BINDING", "0"),
    ("NODE_USE_ENV_PROXY", "1"),
    ("ELECTRON_GET_USE_PROXY", "true"),
    ("OPENSQUILLA_SANDBOX_NETWORK", "proxy_allowlist"),
)
DEFAULT_NO_PROXY_VALUE = (
    "localhost,127.0.0.1,::1,"
    "10.0.0.0/8,"
    "172.16.0.0/12,"
    "192.168.0.0/16"
)


class LinuxProxyBridgeHost:
    """Host-side UDS bridge to the guarded sandbox proxy."""

    def __init__(
        self,
        uds_path: Path,
        upstream_host: str,
        upstream_port: int,
        *,
        script_path: Path | None = None,
        exec_wrapper_path: Path | None = None,
    ) -> None:
        self.uds_path = uds_path
        self.script_path = script_path or (uds_path.parent / "inner_bridge.py")
        self.exec_wrapper_path = exec_wrapper_path or (uds_path.parent / "linux_exec_wrapper.py")
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self._server: asyncio.AbstractServer | None = None
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._active_writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        if self._server is not None:
            return
        self.uds_path.parent.mkdir(parents=True, exist_ok=True)
        self.script_path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(FileNotFoundError):
            self.uds_path.unlink()
        self.script_path.write_text(
            Path(__file__).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        wrapper_source = Path(__file__).with_name("linux_exec_wrapper.py")
        self.exec_wrapper_path.write_text(
            wrapper_source.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        with suppress(OSError):
            os.chmod(self.script_path, 0o600)
        with suppress(OSError):
            os.chmod(self.exec_wrapper_path, 0o600)
        server = await asyncio.start_unix_server(
            self._accept,
            path=str(self.uds_path),
        )
        with suppress(OSError):
            os.chmod(self.uds_path, 0o600)
        self._server = server

    async def stop(self) -> None:
        server = self._server
        if server is not None:
            self._server = None
            server.close()
            await asyncio.sleep(0)

        current_task = asyncio.current_task()
        tasks = [
            task
            for task in self._active_tasks
            if task is not current_task and not task.done()
        ]
        for task in tasks:
            task.cancel()

        writers = tuple(self._active_writers)
        for writer in writers:
            writer.close()
            with suppress(RuntimeError):
                writer.transport.abort()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        for writer in writers:
            with suppress(ConnectionError, RuntimeError):
                await writer.wait_closed()

        if server is not None:
            await server.wait_closed()
        with suppress(FileNotFoundError):
            self.uds_path.unlink()
        with suppress(FileNotFoundError):
            self.script_path.unlink()
        with suppress(FileNotFoundError):
            self.exec_wrapper_path.unlink()

    def _accept(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._active_writers.add(writer)
        task = asyncio.create_task(self._handle(reader, writer))
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self.upstream_host,
                self.upstream_port,
            )
            self._active_writers.add(upstream_writer)
            await _tunnel(reader, writer, upstream_reader, upstream_writer)
        finally:
            self._active_writers.discard(writer)
            writer.close()
            with suppress(ConnectionError, RuntimeError):
                await writer.wait_closed()
            if upstream_writer is not None:
                self._active_writers.discard(upstream_writer)
                upstream_writer.close()
                with suppress(ConnectionError, RuntimeError):
                    await upstream_writer.wait_closed()


async def _handle_inner_connection(
    uds_path: Path,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    uds_writer: asyncio.StreamWriter | None = None
    try:
        uds_reader, uds_writer = await asyncio.open_unix_connection(str(uds_path))
        await _tunnel(reader, writer, uds_reader, uds_writer)
    finally:
        writer.close()
        with suppress(ConnectionError, RuntimeError):
            await writer.wait_closed()
        if uds_writer is not None:
            uds_writer.close()
            with suppress(ConnectionError, RuntimeError):
                await uds_writer.wait_closed()


async def _tunnel(
    left_reader: asyncio.StreamReader,
    left_writer: asyncio.StreamWriter,
    right_reader: asyncio.StreamReader,
    right_writer: asyncio.StreamWriter,
) -> None:
    left_to_right = asyncio.create_task(_pipe(left_reader, right_writer))
    right_to_left = asyncio.create_task(_pipe(right_reader, left_writer))
    tasks = {left_to_right, right_to_left}
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.gather(*done, return_exceptions=True)


async def _pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            chunk = await reader.read(_CHUNK_SIZE)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()


def _child_env(port: int) -> dict[str, str]:
    env = dict(os.environ)
    env.pop(ENV_PROXY_UDS, None)
    env.pop(ENV_PROXY_PORT, None)
    env.pop(ENV_POLICY_B64, None)
    env.pop(ENV_EXEC_WRAPPER, None)
    proxy_url = f"http://{_INNER_PROXY_HOST}:{port}"
    for key in PROXY_ENV_KEYS:
        env[key] = proxy_url
    for key in NO_PROXY_ENV_KEYS:
        env[key] = DEFAULT_NO_PROXY_VALUE
    for key, value in PROXY_CONTROL_ENV:
        env[key] = value
    return env


async def _run_inner(argv: list[str]) -> int:
    if not argv:
        print("linux proxy bridge requires a command after --", file=sys.stderr)
        return 2

    uds_raw = os.environ.get(ENV_PROXY_UDS)
    port_raw = os.environ.get(ENV_PROXY_PORT)
    if not uds_raw or not port_raw:
        print("linux proxy bridge missing internal proxy environment", file=sys.stderr)
        return 2
    try:
        port = int(port_raw)
    except ValueError:
        print("linux proxy bridge proxy port is invalid", file=sys.stderr)
        return 2
    if port <= 0 or port > 65535:
        print("linux proxy bridge proxy port is out of range", file=sys.stderr)
        return 2

    uds_path = Path(uds_raw)
    active_tasks: set[asyncio.Task[None]] = set()

    def accept_inner(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.create_task(_handle_inner_connection(uds_path, reader, writer))
        active_tasks.add(task)
        task.add_done_callback(active_tasks.discard)

    server = await asyncio.start_server(
        accept_inner,
        _INNER_PROXY_HOST,
        port,
    )
    try:
        command = _wrapped_child_argv(argv)
        proc = await asyncio.create_subprocess_exec(
            *command,
            env=_child_env(port),
        )
        return await proc.wait()
    finally:
        server.close()
        await server.wait_closed()
        tasks = [task for task in active_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "--":
        print(
            "usage: python -m opensquilla.sandbox.backend.linux_proxy_bridge -- <cmd>",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(_run_inner(args[1:]))


def _wrapped_child_argv(argv: list[str]) -> list[str]:
    policy_b64 = os.environ.get(ENV_POLICY_B64)
    wrapper = os.environ.get(ENV_EXEC_WRAPPER)
    if not policy_b64 or not wrapper:
        return argv
    return [
        sys.executable,
        wrapper,
        "--policy-b64",
        policy_b64,
        "--",
        *argv,
    ]


if __name__ == "__main__":  # pragma: no cover - exercised through -m
    raise SystemExit(main())


__all__ = [
    "ENV_PROXY_PORT",
    "ENV_PROXY_UDS",
    "ENV_POLICY_B64",
    "ENV_EXEC_WRAPPER",
    "LinuxProxyBridgeHost",
    "PROXY_ENV_KEYS",
    "main",
]
