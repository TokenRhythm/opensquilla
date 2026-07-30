from __future__ import annotations

import asyncio
import dataclasses
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

import opensquilla.sandbox as sandbox
from opensquilla.gateway.routing import build_cli_route_envelope, tool_context_from_envelope
from opensquilla.sandbox import integration as integration_mod
from opensquilla.sandbox.backend import bubblewrap as bubblewrap_mod
from opensquilla.sandbox.backend.bubblewrap import BubblewrapBackend, build_bwrap_argv
from opensquilla.sandbox.backend.linux_readiness import probe_bwrap
from opensquilla.sandbox.backend.seatbelt import render_seatbelt_profile
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.policy import build_policy
from opensquilla.sandbox.run_context import DomainGrant, RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.sandbox.types import (
    MountSpec,
    NetworkMode,
    NetworkProxySpec,
    ResourceLimits,
    SandboxBackendError,
    SandboxPolicy,
    SandboxRequest,
    SandboxResult,
    SecurityLevel,
)
from opensquilla.tools.types import current_tool_context

_UNSET = object()
_BWRAP_PROXY_BRIDGE_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="bubblewrap proxy bridge uses Linux namespaces and mount paths",
)


def _proxy_spec(host: str = "127.0.0.1", port: int = 8080) -> NetworkProxySpec:
    return NetworkProxySpec(host=host, port=port)


def _policy(
    workspace: Path,
    *,
    network: NetworkMode = NetworkMode.PROXY_ALLOWLIST,
    network_proxy: NetworkProxySpec | object = _UNSET,
) -> SandboxPolicy:
    kwargs = {
        "level": SecurityLevel.STANDARD,
        "network": network,
        "mounts": (
            MountSpec(
                host_path=workspace,
                sandbox_path=Path("/workspace"),
                mode="rw",
                required=True,
            ),
        ),
        "workspace_rw": True,
        "tmp_writable": True,
        "limits": ResourceLimits(wall_timeout_s=0.1),
        "env_allowlist": ("PATH",),
        "require_approval": False,
    }
    if network_proxy is not _UNSET:
        kwargs["network_proxy"] = network_proxy
    return SandboxPolicy(**kwargs)


def _request(policy: SandboxPolicy, cwd: Path) -> SandboxRequest:
    return SandboxRequest(
        argv=("sh", "-lc", "echo ok"),
        cwd=cwd,
        action_kind="network.http",
        policy=policy,
        env={"PATH": "/bin"},
    )


@_BWRAP_PROXY_BRIDGE_LINUX_ONLY
def test_bubblewrap_treats_sandbox_paths_as_posix_on_windows(
    tmp_path: Path,
) -> None:
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(
            MountSpec(
                host_path=tmp_path,
                sandbox_path=PureWindowsPath("/workspace"),
                mode="rw",
                required=True,
            ),
        ),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=0.1),
        env_allowlist=("PATH",),
        require_approval=False,
    )

    argv = build_bwrap_argv(_request(policy, tmp_path), binary="bwrap")

    assert tmp_path.as_posix() in argv
    assert "/workspace" not in argv
    assert "\\workspace" not in argv


def test_bubblewrap_proxy_allowlist_without_proxy_fails_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(SandboxBackendError, match="network proxy"):
        build_bwrap_argv(_request(_policy(tmp_path), tmp_path), binary="bwrap")


@pytest.mark.asyncio
async def test_noop_backend_passes_request_proxy_env_to_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.safety.sandbox import SandboxResult as SafetySandboxResult
    from opensquilla.sandbox.backend import noop as noop_mod

    policy = dataclasses.replace(
        _policy(tmp_path, network_proxy=_proxy_spec()),
        env_allowlist=("PATH", "HTTP_PROXY"),
    )
    seen: dict[str, object] = {}

    def _fake_run_sandboxed(cmd, limits=None, *, stdin=None, env=None):
        seen["cmd"] = tuple(cmd)
        seen["stdin"] = stdin
        seen["env"] = dict(env or {})
        return SafetySandboxResult(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(noop_mod, "run_sandboxed", _fake_run_sandboxed)
    request = SandboxRequest(
        argv=(
            sys.executable,
            "-c",
            "import os; print(os.environ.get('HTTP_PROXY', ''))",
        ),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        env={
            "PATH": "/bin",
            "HTTP_PROXY": "http://127.0.0.1:18080",
        },
    )

    result = await noop_mod.NoopBackend().run(request)

    assert result.returncode == 0
    assert seen["stdin"] is None
    assert seen["env"] == {
        "PATH": "/bin",
        "HTTP_PROXY": "http://127.0.0.1:18080",
    }


def test_seatbelt_proxy_allowlist_without_proxy_fails_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(SandboxBackendError, match="network proxy"):
        render_seatbelt_profile(_request(_policy(tmp_path), tmp_path))


@_BWRAP_PROXY_BRIDGE_LINUX_ONLY
def test_bubblewrap_proxy_allowlist_with_proxy_builds_bridge_argv(
    tmp_path: Path,
) -> None:
    policy = _policy(tmp_path, network_proxy=_proxy_spec())
    bridge_uds_path = tmp_path / "bridge" / "proxy.sock"
    bridge_script_path = bridge_uds_path.parent / "inner_bridge.py"

    argv = build_bwrap_argv(
        _request(policy, tmp_path),
        binary="bwrap",
        bridge_uds_path=bridge_uds_path,
        bridge_script_path=bridge_script_path,
    )

    separator = argv.index("--")
    child_argv = argv[separator + 1 :]
    assert "--unshare-net" in argv
    assert child_argv[:3] == [
        "/usr/bin/python3",
        str(bridge_script_path),
        "--",
    ]
    assert child_argv[3:] == ["sh", "-lc", "echo ok"]
    assert sys.executable not in child_argv
    assert "-m" not in child_argv
    assert "opensquilla.sandbox.backend.linux_proxy_bridge" not in child_argv
    assert argv.count("echo ok") == 1
    assert "OPENSQUILLA_SANDBOX_PROXY_UDS" in argv
    assert "OPENSQUILLA_SANDBOX_PROXY_PORT" in argv
    assert "HTTP_PROXY" in argv
    assert "http://127.0.0.1:8080" in argv


@_BWRAP_PROXY_BRIDGE_LINUX_ONLY
def test_bubblewrap_proxy_allowlist_proxy_env_overrides_user_input(
    tmp_path: Path,
) -> None:
    policy = _policy(tmp_path, network_proxy=_proxy_spec())
    request = _request(policy, tmp_path)
    request.env["HTTP_PROXY"] = "http://attacker.invalid:1"

    argv = build_bwrap_argv(request, binary="bwrap")

    assert "http://127.0.0.1:8080" in argv
    assert "http://attacker.invalid:1" not in argv


@_BWRAP_PROXY_BRIDGE_LINUX_ONLY
def test_bubblewrap_proxy_allowlist_injects_package_manager_proxy_env(
    tmp_path: Path,
) -> None:
    policy = _policy(tmp_path, network_proxy=_proxy_spec())

    argv = build_bwrap_argv(_request(policy, tmp_path), binary="bwrap")

    assert "npm_config_proxy" in argv
    assert "NODE_USE_ENV_PROXY" in argv
    assert "GIT_CONFIG_KEY_0" not in argv
    assert "GIT_CONFIG_VALUE_0" not in argv
    assert "OPENSQUILLA_SANDBOX_NETWORK" in argv
    network_index = argv.index("OPENSQUILLA_SANDBOX_NETWORK")
    assert argv[network_index + 1] == "proxy_allowlist"


@_BWRAP_PROXY_BRIDGE_LINUX_ONLY
def test_bubblewrap_preserves_home_when_workspace_mount_is_canonicalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/home/lrk")
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(
            MountSpec(
                host_path=tmp_path,
                sandbox_path=Path("/workspace"),
                mode="rw",
                required=True,
            ),
        ),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=0.1),
        env_allowlist=("PATH", "HOME"),
        require_approval=False,
    )
    request = SandboxRequest(
        argv=("sh", "-lc", "echo $HOME"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        env={"HOME": "/home/lrk"},
    )

    argv = build_bwrap_argv(request, binary="bwrap")

    home_index = argv.index("HOME")
    assert argv[home_index + 1] == "/home/lrk"


@_BWRAP_PROXY_BRIDGE_LINUX_ONLY
def test_bubblewrap_masks_protected_metadata_under_writable_roots(tmp_path: Path) -> None:
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(
            MountSpec(
                host_path=tmp_path,
                sandbox_path=Path("/workspace"),
                mode="rw",
                required=True,
            ),
            MountSpec(
                host_path=tmp_path,
                sandbox_path=tmp_path,
                mode="rw",
                required=False,
            ),
        ),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=0.1),
        env_allowlist=("PATH",),
        require_approval=False,
    )

    argv = build_bwrap_argv(_request(policy, tmp_path), binary="bwrap")

    for protected in (
        tmp_path / ".git",
        tmp_path / ".codex",
        tmp_path / ".agents",
    ):
        target = protected.as_posix()
        assert target in argv
        index = argv.index(target)
        assert argv[index - 1] == "--tmpfs"
        assert any(
            window == ("--remount-ro", target)
            for window in zip(argv, argv[1:], strict=False)
        )


@_BWRAP_PROXY_BRIDGE_LINUX_ONLY
def test_bubblewrap_upgrades_duplicate_host_alias_to_writable(tmp_path: Path) -> None:
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(
            MountSpec(
                host_path=tmp_path,
                sandbox_path=Path("/workspace"),
                mode="rw",
                required=True,
            ),
            MountSpec(
                host_path=tmp_path,
                sandbox_path=tmp_path,
                mode="ro",
                required=False,
            ),
        ),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=0.1),
        env_allowlist=("PATH",),
        require_approval=False,
    )

    argv = build_bwrap_argv(_request(policy, tmp_path), binary="bwrap")

    absolute_target = tmp_path.as_posix()
    assert any(
        window == ("--bind", absolute_target, absolute_target)
        for window in zip(argv, argv[1:], argv[2:], strict=False)
    )
    assert not any(
        window == ("--ro-bind", absolute_target, absolute_target)
        for window in zip(argv, argv[1:], argv[2:], strict=False)
    )


@_BWRAP_PROXY_BRIDGE_LINUX_ONLY
def test_bubblewrap_available_uses_readiness_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        bubblewrap_mod,
        "probe_bwrap",
        lambda: SimpleNamespace(available=True, message="ready"),
    )

    assert BubblewrapBackend().available() is True


def test_bubblewrap_unavailable_when_user_namespace_probe_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        bubblewrap_mod,
        "probe_bwrap",
        lambda: SimpleNamespace(available=False, message="no user namespace"),
    )

    assert BubblewrapBackend().available() is False


@_BWRAP_PROXY_BRIDGE_LINUX_ONLY
@pytest.mark.asyncio
async def test_real_bubblewrap_network_none_cannot_reach_host_loopback(
    tmp_path: Path,
) -> None:
    probe = probe_bwrap()
    if not probe.available:
        pytest.skip(probe.message)
    seen: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        seen.append(await reader.read(4096))
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    socket = next(iter(server.sockets or ()), None)
    assert socket is not None
    host, port = socket.getsockname()[:2]
    policy = _policy(tmp_path, network=NetworkMode.NONE, network_proxy=None)
    code = (
        "import socket\n"
        "s = None\n"
        "try:\n"
        "    s = socket.socket()\n"
        "    s.settimeout(1)\n"
        f"    s.connect(({host!r}, {int(port)}))\n"
        "    print('NETWORK_OPEN')\n"
        "except Exception as exc:\n"
        "    print('NETWORK_BLOCKED', type(exc).__name__)\n"
        "finally:\n"
        "    if s is not None:\n"
        "        s.close()\n"
    )

    try:
        result = await BubblewrapBackend().run(
            SandboxRequest(
                argv=(sys.executable, "-c", code),
                cwd=tmp_path,
                action_kind="shell.exec",
                policy=policy,
                env={},
                session_id="s1",
                run_mode="trusted",
            )
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result.returncode == 0
    assert "NETWORK_BLOCKED" in result.stdout
    assert "NETWORK_OPEN" not in result.stdout
    assert seen == []


@_BWRAP_PROXY_BRIDGE_LINUX_ONLY
@pytest.mark.asyncio
async def test_real_bubblewrap_root_read_workspace_write_and_external_readonly(
    tmp_path: Path,
) -> None:
    probe = probe_bwrap()
    if not probe.available:
        pytest.skip(probe.message)
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        workspace,
        SandboxSettings(
            backend="bubblewrap",
            network_default="none",
            exclude_slash_tmp=True,
            exclude_tmpdir_env_var=True,
        ),
    )
    policy = dataclasses.replace(
        policy,
        network=NetworkMode.NONE,
        limits=dataclasses.replace(policy.limits, wall_timeout_s=5.0),
    )
    workspace_probe = workspace / "inside.txt"
    outside_probe = outside / "outside.txt"
    code = (
        "from pathlib import Path\n"
        "print('ROOT_LISTED', Path('/etc').is_dir())\n"
        "print('HOST_READ', bool(Path('/etc/hosts').read_text(encoding='utf-8')))\n"
        f"Path({str(workspace_probe)!r}).write_text('inside', encoding='utf-8')\n"
        "print('WORKSPACE_WRITTEN')\n"
        "try:\n"
        f"    Path({str(outside_probe)!r}).write_text('outside', encoding='utf-8')\n"
        "    print('EXTERNAL_WRITTEN')\n"
        "except OSError as exc:\n"
        "    print('EXTERNAL_BLOCKED', type(exc).__name__)\n"
    )

    result = await BubblewrapBackend().run(
        SandboxRequest(
            argv=(sys.executable, "-c", code),
            cwd=workspace,
            action_kind="shell.exec",
            policy=policy,
            env={},
            session_id="root-read-acceptance",
            run_mode="standard",
        )
    )

    assert result.returncode == 0
    assert "ROOT_LISTED True" in result.stdout
    assert "HOST_READ True" in result.stdout
    assert "WORKSPACE_WRITTEN" in result.stdout
    assert "EXTERNAL_BLOCKED" in result.stdout
    assert "EXTERNAL_WRITTEN" not in result.stdout
    assert workspace_probe.read_text(encoding="utf-8") == "inside"
    assert not outside_probe.exists()


@_BWRAP_PROXY_BRIDGE_LINUX_ONLY
@pytest.mark.asyncio
async def test_real_bubblewrap_leaves_unreadable_system_file_to_os_permissions(
    tmp_path: Path,
) -> None:
    probe = probe_bwrap()
    if not probe.available:
        pytest.skip(probe.message)
    policy = _policy(tmp_path, network=NetworkMode.NONE, network_proxy=None)
    code = (
        "from pathlib import Path\n"
        "target = Path('/' + 'etc') / 'shadow'\n"
        "try:\n"
        "    print(target.read_text(encoding='utf-8')[:32])\n"
        "except Exception as exc:\n"
        "    print('OS_READ_BLOCKED', type(exc).__name__)\n"
    )

    result = await BubblewrapBackend().run(
        SandboxRequest(
            argv=(sys.executable, "-c", code),
            cwd=tmp_path,
            action_kind="shell.exec",
            policy=policy,
            env={},
            session_id="s1",
            run_mode="trusted",
        )
    )

    assert result.returncode == 0
    assert "OS_READ_BLOCKED PermissionError" in result.stdout
    assert "root:" not in result.stdout


@_BWRAP_PROXY_BRIDGE_LINUX_ONLY
@pytest.mark.asyncio
async def test_real_bubblewrap_proxy_allowlist_reaches_managed_proxy(
    tmp_path: Path,
) -> None:
    probe = probe_bwrap()
    if not probe.available:
        pytest.skip(probe.message)
    seen: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        seen.append(await reader.read(4096))
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"ok"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    socket = next(iter(server.sockets or ()), None)
    assert socket is not None
    host, port = socket.getsockname()[:2]
    policy = _policy(
        tmp_path,
        network=NetworkMode.PROXY_ALLOWLIST,
        network_proxy=NetworkProxySpec(host=str(host), port=int(port)),
    )
    code = (
        "import os, socket, urllib.parse\n"
        "proxy = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')\n"
        "print('PROXY', proxy)\n"
        "print('NPM_PROXY', os.environ.get('npm_config_proxy', ''))\n"
        "print('NODE_USE_ENV_PROXY', os.environ.get('NODE_USE_ENV_PROXY', ''))\n"
        "print('OPENSQUILLA_SANDBOX_NETWORK', os.environ.get('OPENSQUILLA_SANDBOX_NETWORK', ''))\n"
        "print('CODEX_NETWORK_PROXY_ACTIVE', os.environ.get('CODEX_NETWORK_PROXY_ACTIVE', ''))\n"
        "print('CODEX_NETWORK_ALLOW_LOCAL_BINDING', "
        "os.environ.get('CODEX_NETWORK_ALLOW_LOCAL_BINDING', ''))\n"
        "print('GIT_SSL_KEY', os.environ.get('GIT_CONFIG_KEY_0', ''))\n"
        "url = urllib.parse.urlparse(proxy)\n"
        "s = socket.create_connection((url.hostname, url.port), timeout=3)\n"
        "s.sendall(b'GET http://allowed.test/path HTTP/1.1\\r\\nHost: allowed.test\\r\\n\\r\\n')\n"
        "print(s.recv(4096).decode('latin1'))\n"
        "s.close()\n"
    )

    try:
        result = await BubblewrapBackend().run(
            SandboxRequest(
                argv=(sys.executable, "-c", code),
                cwd=tmp_path,
                action_kind="shell.exec",
                policy=policy,
                env={"HTTP_PROXY": "http://attacker.invalid:1"},
                session_id="s1",
                run_mode="trusted",
            )
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result.returncode == 0
    assert "PROXY http://127.0.0.1:" in result.stdout
    assert "NPM_PROXY http://127.0.0.1:" in result.stdout
    assert "NODE_USE_ENV_PROXY 1" in result.stdout
    assert "OPENSQUILLA_SANDBOX_NETWORK proxy_allowlist" in result.stdout
    assert "CODEX_NETWORK_PROXY_ACTIVE 1" in result.stdout
    assert "CODEX_NETWORK_ALLOW_LOCAL_BINDING 0" in result.stdout
    assert "GIT_SSL_KEY \n" in result.stdout
    assert "attacker.invalid" not in result.stdout
    assert "HTTP/1.1 200 OK" in result.stdout
    assert seen == [b"GET http://allowed.test/path HTTP/1.1\r\nHost: allowed.test\r\n\r\n"]


def test_linux_proxy_routing_rewrites_proxy_env_to_inner_loopback() -> None:
    from opensquilla.sandbox.backend.linux_proxy_routing import proxy_env_for_inner_port

    env = proxy_env_for_inner_port(
        base_env={"HTTP_PROXY": "http://127.0.0.1:3128"},
        port=18080,
    )

    assert env["HTTP_PROXY"] == "http://127.0.0.1:18080"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:18080"


@_BWRAP_PROXY_BRIDGE_LINUX_ONLY
@pytest.mark.asyncio
async def test_bubblewrap_run_invokes_linux_helper_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(tmp_path, network=NetworkMode.NONE, network_proxy=None)
    seen: dict[str, object] = {}

    async def fake_helper(payload):
        seen["payload"] = payload
        return {
            "returncode": 0,
            "stdout": "ok\n",
            "stderr": "",
            "wallTimeS": 0.01,
            "timedOut": False,
            "truncatedStdout": False,
            "truncatedStderr": False,
        }

    monkeypatch.setattr(
        bubblewrap_mod,
        "probe_bwrap",
        lambda: SimpleNamespace(available=True, message="ready"),
    )
    monkeypatch.setattr(bubblewrap_mod, "_run_linux_helper_payload", fake_helper)

    result = await BubblewrapBackend(binary="bwrap").run(_request(policy, tmp_path))

    assert result.returncode == 0
    assert result.stdout == "ok\n"
    payload = seen["payload"]
    assert getattr(payload, "operation_type") == "process"
    assert getattr(payload, "process").argv == ["sh", "-lc", "echo ok"]


@_BWRAP_PROXY_BRIDGE_LINUX_ONLY
@pytest.mark.asyncio
async def test_bubblewrap_run_starts_and_stops_proxy_bridge_for_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(tmp_path, network_proxy=_proxy_spec(port=18080))
    events: list[str] = []
    captured: dict[str, object] = {}

    class FakeBridge:
        def __init__(self, uds_path: Path, upstream_host: str, upstream_port: int) -> None:
            self.uds_path = uds_path
            self.script_path = uds_path.parent / "inner_bridge.py"
            self.upstream_host = upstream_host
            self.upstream_port = upstream_port
            captured["bridge"] = (uds_path, upstream_host, upstream_port)

        async def start(self) -> None:
            events.append("bridge.start")

        async def stop(self) -> None:
            events.append("bridge.stop")

    async def fake_helper(payload):
        events.append("helper.run")
        captured["payload"] = payload
        return {
            "returncode": 0,
            "stdout": "ok\n",
            "stderr": "",
            "wallTimeS": 0.01,
            "timedOut": False,
            "truncatedStdout": False,
            "truncatedStderr": False,
        }

    monkeypatch.setattr(
        bubblewrap_mod,
        "probe_bwrap",
        lambda: SimpleNamespace(available=True, message="ready"),
    )
    monkeypatch.setattr(bubblewrap_mod, "LinuxProxyBridgeHost", FakeBridge)
    monkeypatch.setattr(bubblewrap_mod, "_run_linux_helper_payload", fake_helper)

    result = await BubblewrapBackend(binary="bwrap").run(_request(policy, tmp_path))

    assert events == ["bridge.start", "helper.run", "bridge.stop"]
    assert result.returncode == 0
    bridge = captured["bridge"]
    assert isinstance(bridge, tuple)
    assert bridge[1:] == ("127.0.0.1", 18080)
    payload = captured["payload"]
    bridge_payload = getattr(payload, "policy")["linuxProxyBridge"]
    assert bridge_payload["udsPath"].endswith("/proxy.sock")
    assert bridge_payload["scriptPath"].endswith("/inner_bridge.py")
    assert "execWrapperPath" not in bridge_payload
    assert bridge_payload["port"] == 18080


@pytest.mark.asyncio
async def test_linux_helper_payload_times_out_outer_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = bubblewrap_mod.build_process_helper_payload(
        _request(_policy(tmp_path, network=NetworkMode.NONE, network_proxy=None), tmp_path)
    )
    payload = dataclasses.replace(
        payload,
        policy={**payload.policy, "wallTimeoutS": 0.01},
    )
    terminated: list[int] = []

    class _Proc:
        pid = 12345
        returncode = None
        stdout = None
        stderr = None

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

    async def fake_create_subprocess_exec(*argv, **kwargs):
        assert kwargs["start_new_session"] is True
        return _Proc()

    async def fake_terminate(proc):
        terminated.append(proc.pid)
        return b"", b""

    monkeypatch.setattr(
        bubblewrap_mod.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(
        bubblewrap_mod,
        "_outer_helper_timeout_s",
        lambda payload: 0.01,
    )
    monkeypatch.setattr(bubblewrap_mod, "_terminate_process_group", fake_terminate)

    with pytest.raises(SandboxBackendError, match="linux helper timed out"):
        await bubblewrap_mod._run_linux_helper_payload(payload)

    assert terminated == [12345]


@_BWRAP_PROXY_BRIDGE_LINUX_ONLY
@pytest.mark.asyncio
async def test_linux_proxy_bridge_host_writes_and_removes_inner_script(
    tmp_path: Path,
) -> None:
    bridge = bubblewrap_mod.LinuxProxyBridgeHost(
        tmp_path / "proxy.sock",
        "127.0.0.1",
        9,
    )

    await bridge.start()
    try:
        assert bridge.script_path.exists()
        script = bridge.script_path.read_text(encoding="utf-8")
        assert "def main(" in script
        assert "asyncio.start_server" in script
    finally:
        await bridge.stop()

    assert not bridge.script_path.exists()


def test_seatbelt_proxy_allowlist_with_proxy_renders_proxy_only_profile(
    tmp_path: Path,
) -> None:
    policy = _policy(tmp_path, network_proxy=_proxy_spec())

    profile = render_seatbelt_profile(_request(policy, tmp_path))

    assert "(allow network-outbound" in profile
    assert "localhost:8080" in profile
    assert "127.0.0.1:8080" not in profile
    assert "(allow network*)" not in profile
    assert "(deny network*)" not in profile


@pytest.mark.asyncio
async def test_run_under_backend_populates_proxy_from_current_run_context(
    tmp_path: Path,
) -> None:
    seen: dict[str, object] = {}

    class FakeBackend:
        name = "fake"

        async def run(self, request: SandboxRequest) -> SandboxResult:
            proxy = request.policy.network_proxy
            seen["proxy"] = proxy
            assert proxy is not None
            reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
            try:
                writer.write(
                    b"GET http://127.0.0.1/path HTTP/1.1\r\n"
                    b"\r\n"
                )
                await writer.drain()
                seen["proxy_response"] = await reader.read(4096)
            finally:
                writer.close()
                await writer.wait_closed()
            return SandboxResult(
                returncode=0,
                stdout="ok",
                stderr="",
                wall_time_s=0.0,
                backend_used="fake",
            )

    runtime = SimpleNamespace(backend=FakeBackend())
    policy = _policy(tmp_path, network_proxy=None)
    run_context = RunContext(
        run_mode=RunMode.STANDARD,
        domains=(DomainGrant(domain="allowed.test"),),
    )
    envelope = build_cli_route_envelope(
        session_key="agent:main:webchat:abc",
        run_mode="standard",
    )
    envelope.metadata["sandbox_run_context"] = run_context.to_origin_payload()
    ctx = tool_context_from_envelope(envelope, workspace_dir=str(tmp_path))
    token = current_tool_context.set(ctx)
    try:
        result = await integration_mod.run_under_backend(
            _request(policy, tmp_path),
            runtime=runtime,
        )
    finally:
        current_tool_context.reset(token)

    assert result.stdout == "ok"
    assert seen["proxy_response"].startswith(b"HTTP/1.1 403")
    proxy = seen["proxy"]
    assert isinstance(proxy, NetworkProxySpec)
    assert proxy.host == "127.0.0.1"
    assert proxy.port > 0


@pytest.mark.asyncio
async def test_run_under_backend_proxy_allowlist_without_context_fails_closed(
    tmp_path: Path,
) -> None:
    class FakeBackend:
        name = "fake"

        async def run(self, request: SandboxRequest) -> SandboxResult:
            raise AssertionError("backend should not run without proxy context")

    runtime = SimpleNamespace(backend=FakeBackend())

    with pytest.raises(SandboxBackendError, match="Run Context"):
        await integration_mod.run_under_backend(
            _request(_policy(tmp_path, network_proxy=None), tmp_path),
            runtime=runtime,
        )


@pytest.mark.asyncio
async def test_non_windows_proxy_allowlist_skips_platform_network_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeBackend:
        name = "bubblewrap"

        async def run(self, request: SandboxRequest) -> SandboxResult:
            return SandboxResult(
                returncode=0,
                stdout="ok",
                stderr="",
                wall_time_s=0.0,
                backend_used="bubblewrap",
            )

    async def fake_prepare_platform_network_boundary(
        request: SandboxRequest,
        runtime: object,
    ) -> object:
        calls.append("prepare")
        return None

    monkeypatch.setattr(
        integration_mod,
        "_prepare_platform_network_boundary",
        fake_prepare_platform_network_boundary,
    )

    policy = _policy(
        tmp_path,
        network_proxy=NetworkProxySpec(host="127.0.0.1", port=18080),
    )
    result = await integration_mod.run_under_backend(
        _request(policy, tmp_path),
        runtime=SimpleNamespace(backend=FakeBackend()),
    )

    assert result.stdout == "ok"
    assert calls == []


def test_policy_positional_description_keeps_legacy_binding(
    tmp_path: Path,
) -> None:
    policy = SandboxPolicy(
        SecurityLevel.STANDARD,
        NetworkMode.NONE,
        (
            MountSpec(
                host_path=tmp_path,
                sandbox_path=Path("/workspace"),
                mode="rw",
                required=True,
            ),
        ),
        True,
        True,
        ResourceLimits(wall_timeout_s=0.1),
        ("PATH",),
        False,
        "legacy description",
    )

    assert policy.description == "legacy description"
    assert policy.network_proxy is None


def test_package_reexports_network_proxy_spec() -> None:
    assert sandbox.NetworkProxySpec is NetworkProxySpec


def test_policy_summary_includes_network_proxy_none(tmp_path: Path) -> None:
    summary = _policy(tmp_path).summary()

    assert summary.get("network_proxy", _UNSET) is None


def test_policy_summary_includes_network_proxy_object(tmp_path: Path) -> None:
    summary = _policy(
        tmp_path,
        network_proxy=_proxy_spec(host="127.0.0.1", port=18080),
    ).summary()

    assert summary["network_proxy"] == {"host": "127.0.0.1", "port": 18080}


def test_windows_proxy_allowlist_preflight_uses_marker_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.backend import windows_default_support as support_mod

    calls: list[tuple[int, ...]] = []

    def _fake_probe_windows_default_support(*, proxy_ports=(), **kwargs):
        calls.append(tuple(proxy_ports))
        return SimpleNamespace(proxy_allowlist_enforced=tuple(proxy_ports) == (48123,))

    monkeypatch.setattr(
        integration_mod,
        "_windows_allowed_proxy_ports",
        lambda runtime: (48123,),
    )
    monkeypatch.setattr(
        support_mod,
        "probe_windows_default_support",
        _fake_probe_windows_default_support,
    )

    assert integration_mod._windows_proxy_allowlist_enforced(
        SimpleNamespace(backend=SimpleNamespace(name="windows_default")),
    )
    assert calls == [(48123,)]


@pytest.mark.asyncio
async def test_windows_proxy_allowlist_starts_proxy_on_allowed_marker_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports_seen: list[int] = []

    class FakeBackend:
        name = "windows_default"

        async def run(self, request: SandboxRequest) -> SandboxResult:
            assert request.policy.network_proxy is not None
            ports_seen.append(request.policy.network_proxy.port)
            return SandboxResult(
                returncode=0,
                stdout="ok",
                stderr="",
                wall_time_s=0.0,
                backend_used="windows_default",
            )

    monkeypatch.setattr(
        integration_mod,
        "_windows_proxy_allowlist_enforced",
        lambda runtime, proxy_ports=(): tuple(proxy_ports) == (48123,),
    )
    monkeypatch.setattr(
        integration_mod,
        "_windows_allowed_proxy_ports",
        lambda runtime: (48123,),
    )

    policy = _policy(tmp_path, network_proxy=None)
    run_context = RunContext(run_mode=RunMode.TRUSTED)
    envelope = build_cli_route_envelope(
        session_key="agent:main:webchat:abc",
        run_mode="trusted",
    )
    envelope.metadata["sandbox_run_context"] = run_context.to_origin_payload()
    ctx = tool_context_from_envelope(envelope, workspace_dir=str(tmp_path))
    token = current_tool_context.set(ctx)
    try:
        result = await integration_mod.run_under_backend(
            _request(policy, tmp_path),
            runtime=SimpleNamespace(backend=FakeBackend()),
        )
    finally:
        current_tool_context.reset(token)

    assert result.stdout == "ok"
    assert ports_seen == [48123]
