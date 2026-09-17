from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.sandbox.backend import seatbelt as seatbelt_mod
from opensquilla.sandbox.backend.bubblewrap import BubblewrapBackend
from opensquilla.sandbox.backend.seatbelt import SeatbeltBackend
from opensquilla.sandbox.types import (
    MountSpec,
    NetworkMode,
    NetworkProxySpec,
    ResourceLimits,
    SandboxPolicy,
    SandboxRequest,
    SecurityLevel,
)
from opensquilla.tools.builtin import shell

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="Seatbelt background test models macOS/POSIX paths",
)


class _FakeProcess:
    pid = 12345
    returncode = None
    stdout = None
    stderr = None


def _owned_fake_process() -> _FakeProcess:
    process = _FakeProcess()
    owner = shell.ProcessTreeOwner(
        process=process,
        pid=process.pid,
        ownership_error="synthetic test process",
    )
    setattr(process, "_opensquilla_process_tree_owner", owner)
    return process


def _request(workspace: Path) -> SandboxRequest:
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(
            MountSpec(
                host_path=workspace,
                sandbox_path=Path("/workspace"),
                mode="rw",
                required=True,
            ),
        ),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=60.0),
        env_allowlist=("PATH", "TMPDIR"),
        require_approval=False,
    )
    return SandboxRequest(
        argv=("sh", "-lc", "sleep 10"),
        cwd=workspace,
        action_kind="shell.background",
        policy=policy,
        env={"PATH": "/bin:/usr/bin"},
    )


def _proxy_request(workspace: Path) -> SandboxRequest:
    request = _request(workspace)
    policy = request.policy
    return request.with_policy(
        SandboxPolicy(
            level=policy.level,
            network=NetworkMode.PROXY_ALLOWLIST,
            mounts=policy.mounts,
            workspace_rw=policy.workspace_rw,
            tmp_writable=policy.tmp_writable,
            limits=policy.limits,
            env_allowlist=policy.env_allowlist,
            require_approval=policy.require_approval,
            network_proxy=NetworkProxySpec(host="127.0.0.1", port=43128),
        )
    )


def _limited_request(workspace: Path) -> SandboxRequest:
    request = _request(workspace)
    policy = request.policy
    return request.with_policy(
        SandboxPolicy(
            level=policy.level,
            network=policy.network,
            mounts=policy.mounts,
            workspace_rw=policy.workspace_rw,
            tmp_writable=policy.tmp_writable,
            limits=ResourceLimits(
                cpu_seconds=7,
                memory_mb=128,
                pids=9,
                wall_timeout_s=12.0,
            ),
            env_allowlist=policy.env_allowlist,
            require_approval=policy.require_approval,
        )
    )


@pytest.mark.asyncio
async def test_spawn_sandboxed_background_supports_seatbelt_and_cleans_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*argv: str, **kwargs: object) -> _FakeProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _owned_fake_process()

    monkeypatch.setattr(
        seatbelt_mod,
        "_sandbox_exec_binary",
        lambda binary=None: "/usr/bin/sandbox-exec",
    )
    monkeypatch.setattr(
        shell,
        "create_owned_subprocess_exec",
        fake_create_subprocess_exec,
    )

    spawned = await shell._spawn_sandboxed_background_process(
        runtime=SimpleNamespace(backend=SeatbeltBackend()),
        request=_request(tmp_path),
    )

    argv = captured["argv"]
    assert isinstance(argv, tuple)
    assert argv[:3] == ("/usr/bin/sandbox-exec", "-f", argv[2])
    profile_path = Path(argv[2])
    assert profile_path.exists()
    assert argv[3:] == ("sh", "-lc", "sleep 10")
    kwargs = captured["kwargs"]
    assert kwargs["cwd"] == str(tmp_path)
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["PATH"] == "/bin:/usr/bin"
    assert "TMPDIR" in env
    assert spawned.process.pid == 12345

    session = shell._BgSession(
        session_id="seatbelt",
        command="sleep 10",
        process=spawned.process,
        cleanup_callbacks=spawned.cleanup_callbacks,
    )
    shell._finalize_bg_session(session)

    assert not profile_path.exists()


@pytest.mark.asyncio
async def test_spawn_sandboxed_background_seatbelt_uses_managed_proxy_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*argv: str, **kwargs: object) -> _FakeProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _owned_fake_process()

    monkeypatch.setattr(
        seatbelt_mod,
        "_sandbox_exec_binary",
        lambda binary=None: "/usr/bin/sandbox-exec",
    )
    monkeypatch.setattr(
        shell,
        "create_owned_subprocess_exec",
        fake_create_subprocess_exec,
    )

    request = _proxy_request(tmp_path)
    request.env["HTTP_PROXY"] = "http://attacker.invalid:1"

    spawned = await shell._spawn_sandboxed_background_process(
        runtime=SimpleNamespace(backend=SeatbeltBackend()),
        request=request,
    )
    try:
        kwargs = captured["kwargs"]
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["HTTP_PROXY"] == "http://127.0.0.1:43128"
        assert env["npm_config_proxy"] == "http://127.0.0.1:43128"
        assert env["OPENSQUILLA_SANDBOX_NETWORK"] == "proxy_allowlist"
        assert "http://attacker.invalid:1" not in env.values()
    finally:
        for callback in spawned.cleanup_callbacks:
            callback()


@pytest.mark.asyncio
async def test_spawn_sandboxed_background_starts_linux_proxy_bridge_for_bubblewrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    captured: dict[str, object] = {}

    class FakeBridge:
        def __init__(
            self,
            uds_path: Path,
            upstream_host: str,
            upstream_port: int,
            *,
            script_path: Path | None = None,
        ) -> None:
            captured["bridge"] = (uds_path, upstream_host, upstream_port, script_path)
            self.uds_path = uds_path
            self.script_path = script_path or (uds_path.parent / "inner_bridge.py")
            self.exec_wrapper_path = uds_path.parent / "linux_exec_wrapper.py"
            self.upstream_host = upstream_host
            self.upstream_port = upstream_port

        async def start(self) -> None:
            events.append("bridge.start")

        async def stop(self) -> None:
            events.append("bridge.stop")

    async def fake_create_subprocess_exec(*argv: str, **kwargs: object) -> _FakeProcess:
        events.append("spawn")
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _owned_fake_process()

    monkeypatch.setattr(shell, "LinuxProxyBridgeHost", FakeBridge, raising=False)
    monkeypatch.setattr(
        shell,
        "create_owned_subprocess_exec",
        fake_create_subprocess_exec,
    )

    spawned = await shell._spawn_sandboxed_background_process(
        runtime=SimpleNamespace(backend=BubblewrapBackend(binary="bwrap")),
        request=_proxy_request(tmp_path),
    )

    assert events == ["bridge.start", "spawn"]
    argv = captured["argv"]
    assert isinstance(argv, tuple)
    assert str(captured["bridge"][0]) in argv
    assert "OPENSQUILLA_SANDBOX_PROXY_UDS" in argv
    assert "OPENSQUILLA_SANDBOX_POLICY_B64" in argv
    assert "HTTP_PROXY" in argv
    assert "http://127.0.0.1:43128" in argv
    assert spawned.async_cleanup_callbacks

    await spawned.async_cleanup_callbacks[0]()

    assert events == ["bridge.start", "spawn", "bridge.stop"]


@pytest.mark.asyncio
async def test_spawn_sandboxed_background_applies_bubblewrap_resource_limits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    async def fake_create_subprocess_exec(*argv: str, **kwargs: object) -> _FakeProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _owned_fake_process()

    def fake_resource_preexec_from_limits(limits: ResourceLimits) -> object:
        captured["limits"] = limits
        return sentinel

    monkeypatch.setattr(
        shell,
        "create_owned_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(
        shell,
        "resource_preexec_from_limits",
        fake_resource_preexec_from_limits,
        raising=False,
    )

    await shell._spawn_sandboxed_background_process(
        runtime=SimpleNamespace(backend=BubblewrapBackend(binary="bwrap")),
        request=_limited_request(tmp_path),
    )

    assert captured["limits"] == _limited_request(tmp_path).policy.limits
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["preexec_fn"] is sentinel


@pytest.mark.asyncio
async def test_spawn_sandboxed_background_wraps_bubblewrap_command_for_inner_seccomp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*argv: str, **kwargs: object) -> _FakeProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _owned_fake_process()

    monkeypatch.setattr(
        shell,
        "create_owned_subprocess_exec",
        fake_create_subprocess_exec,
    )

    spawned = await shell._spawn_sandboxed_background_process(
        runtime=SimpleNamespace(backend=BubblewrapBackend(binary="bwrap")),
        request=_request(tmp_path),
    )

    argv = captured["argv"]
    assert isinstance(argv, tuple)
    assert any(item.endswith("linux_exec_wrapper.py") for item in argv)
    assert "--policy-b64" in argv
    assert argv[-4:] == ("--", "sh", "-lc", "sleep 10")
    assert spawned.cleanup_callbacks

    wrapper_path = next(Path(item) for item in argv if item.endswith("linux_exec_wrapper.py"))
    assert wrapper_path.exists()

    session = shell._BgSession(
        session_id="bwrap",
        command="sleep 10",
        process=spawned.process,
        cleanup_callbacks=spawned.cleanup_callbacks,
        async_cleanup_callbacks=spawned.async_cleanup_callbacks,
    )
    shell._finalize_bg_session(session)

    assert not wrapper_path.exists()


@pytest.mark.asyncio
async def test_spawn_sandboxed_background_skips_proc_when_bwrap_probe_disables_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*argv: str, **kwargs: object) -> _FakeProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _owned_fake_process()

    monkeypatch.setattr(
        shell,
        "create_owned_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(
        shell,
        "probe_bwrap",
        lambda: SimpleNamespace(available=True, supports_proc=False),
        raising=False,
    )

    await shell._spawn_sandboxed_background_process(
        runtime=SimpleNamespace(backend=BubblewrapBackend(binary="bwrap")),
        request=_request(tmp_path),
    )

    argv = captured["argv"]
    assert isinstance(argv, tuple)
    assert "--proc" not in argv
