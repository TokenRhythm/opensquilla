from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from opensquilla.sandbox.backend import linux_helper
from opensquilla.sandbox.backend.linux_filesystem import run_filesystem_payload
from opensquilla.sandbox.backend.linux_helper import build_outer_bwrap_command
from opensquilla.sandbox.backend.linux_payload import (
    FilesystemHelperPayload,
    HelperPayload,
    ProcessHelperPayload,
    encode_payload,
)
from opensquilla.sandbox.backend.linux_process import run_process_payload
from opensquilla.sandbox.backend.linux_protected_create import (
    SyntheticMountCleanupTarget,
    cleanup_protected_create_registrations,
    cleanup_synthetic_mount_registrations,
    cleanup_synthetic_mount_targets,
    register_protected_create_targets,
    register_synthetic_mount_targets,
)
from opensquilla.sandbox.permissions import FileSystemAccess

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux helper tests require POSIX process-group and seccomp semantics",
)


def test_filesystem_profile_from_legacy_payload_defaults_to_deny() -> None:
    policy = linux_helper._policy_from_payload(
        {
            "fileSystem": {
                "entries": [],
                "deniedReadGlobs": [],
            }
        }
    )

    assert policy.file_system is not None
    assert policy.file_system.default_access is FileSystemAccess.DENY


@pytest.mark.asyncio
async def test_run_process_payload_captures_stdout_and_stderr(tmp_path) -> None:
    payload = HelperPayload(
        operation_type="process",
        action_kind="shell.exec",
        run_mode="trusted",
        session_id="s1",
        cwd=str(tmp_path),
        env={},
        policy={"wallTimeoutS": 5.0},
        process=ProcessHelperPayload(
            argv=[
                sys.executable,
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr)",
            ]
        ),
        filesystem=None,
    )

    result = await run_process_payload(payload)

    assert result["returncode"] == 0
    assert result["stdout"] == "out\n"
    assert result["stderr"] == "err\n"
    assert result["timedOut"] is False


@pytest.mark.asyncio
async def test_run_process_payload_passes_resource_preexec_to_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}

    class _Proc:
        returncode = 0

        async def communicate(self, input=None):
            return b"", b""

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(
        "opensquilla.sandbox.backend.linux_process.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    payload = HelperPayload(
        operation_type="process",
        action_kind="shell.exec",
        run_mode="trusted",
        session_id="s1",
        cwd=str(tmp_path),
        env={},
        policy={
            "cpuSeconds": 7,
            "memoryMb": 64,
            "pids": 23,
            "wallTimeoutS": 5.0,
        },
        process=ProcessHelperPayload(argv=[sys.executable, "-c", "print('ok')"]),
        filesystem=None,
    )

    result = await run_process_payload(payload)

    assert result["returncode"] == 0
    kwargs = captured["kwargs"]
    assert callable(kwargs["preexec_fn"])


@pytest.mark.asyncio
async def test_run_process_payload_passes_network_seccomp_to_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}

    class _Proc:
        returncode = 0

        async def communicate(self, input=None):
            return b"", b""

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["kwargs"] = kwargs
        return _Proc()

    def fake_process_preexec_from_policy(policy):
        captured["policy"] = policy
        return lambda: None

    monkeypatch.setattr(
        "opensquilla.sandbox.backend.linux_process.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(
        "opensquilla.sandbox.backend.linux_process.process_preexec_from_policy",
        fake_process_preexec_from_policy,
        raising=False,
    )
    payload = HelperPayload(
        operation_type="process",
        action_kind="shell.exec",
        run_mode="trusted",
        session_id="s1",
        cwd=str(tmp_path),
        env={},
        policy={
            "network": "proxy_allowlist",
            "cpuSeconds": 7,
            "memoryMb": 64,
            "pids": 23,
            "wallTimeoutS": 5.0,
        },
        process=ProcessHelperPayload(argv=[sys.executable, "-c", "print('ok')"]),
        filesystem=None,
    )

    await run_process_payload(payload)

    assert captured["policy"] == payload.policy
    assert callable(captured["kwargs"]["preexec_fn"])


@pytest.mark.asyncio
async def test_run_process_payload_times_out(tmp_path) -> None:
    payload = HelperPayload(
        operation_type="process",
        action_kind="shell.exec",
        run_mode="trusted",
        session_id="s1",
        cwd=str(tmp_path),
        env={},
        policy={"wallTimeoutS": 0.01},
        process=ProcessHelperPayload(
            argv=[sys.executable, "-c", "import time; time.sleep(5)"]
        ),
        filesystem=None,
    )

    result = await run_process_payload(payload)

    assert result["returncode"] == 124
    assert result["timedOut"] is True
    assert "timed out" in result["stderr"]


@pytest.mark.asyncio
async def test_run_process_payload_proxy_mode_denies_af_unix_socket(tmp_path) -> None:
    payload = HelperPayload(
        operation_type="process",
        action_kind="shell.exec",
        run_mode="trusted",
        session_id="s1",
        cwd=str(tmp_path),
        env={},
        policy={"network": "proxy_allowlist", "wallTimeoutS": 5.0},
        process=ProcessHelperPayload(
            argv=[
                sys.executable,
                "-c",
                (
                    "import socket, sys\n"
                    "try:\n"
                    "    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
                    "except PermissionError:\n"
                    "    print('af_unix_denied')\n"
                    "    raise SystemExit(0)\n"
                    "except OSError as exc:\n"
                    "    print(type(exc).__name__)\n"
                    "    raise SystemExit(2)\n"
                    "print('unexpected_af_unix')\n"
                    "raise SystemExit(1)\n"
                ),
            ]
        ),
        filesystem=None,
    )

    result = await run_process_payload(payload)

    assert result["returncode"] == 0
    assert result["stdout"] == "af_unix_denied\n"


@pytest.mark.asyncio
async def test_run_process_payload_restricted_mode_allows_af_unix_only(tmp_path) -> None:
    payload = HelperPayload(
        operation_type="process",
        action_kind="shell.exec",
        run_mode="trusted",
        session_id="s1",
        cwd=str(tmp_path),
        env={},
        policy={"network": "none", "wallTimeoutS": 5.0},
        process=ProcessHelperPayload(
            argv=[
                sys.executable,
                "-c",
                (
                    "import socket\n"
                    "socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()\n"
                    "try:\n"
                    "    socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
                    "except PermissionError:\n"
                    "    print('ip_denied')\n"
                    "    raise SystemExit(0)\n"
                    "print('unexpected_ip')\n"
                    "raise SystemExit(1)\n"
                ),
            ]
        ),
        filesystem=None,
    )

    result = await run_process_payload(payload)

    assert result["returncode"] == 0
    assert result["stdout"] == "ip_denied\n"


@pytest.mark.asyncio
async def test_run_filesystem_payload_writes_worker_payload_and_parses_result(
    tmp_path,
) -> None:
    target = tmp_path / "out.txt"
    worker_payload_path = tmp_path / "payload.json"
    payload = HelperPayload(
        operation_type="filesystem",
        action_kind="fs.worker.write_text",
        run_mode="trusted",
        session_id="s1",
        cwd=str(tmp_path),
        env={},
        policy={"wallTimeoutS": 5.0},
        process=None,
        filesystem=FilesystemHelperPayload(
            kind="write_text",
            worker_payload_path=str(worker_payload_path),
            worker_payload={
                "kind": "write_text",
                "path": str(target),
                "content": "hello",
            },
        ),
    )

    result = await run_filesystem_payload(payload)

    assert result["message"] == f"Written 5 bytes to {target}"
    assert result["created"] is True
    assert target.read_text(encoding="utf-8") == "hello"


def test_linux_helper_inner_process_prints_json_result(tmp_path, capsys) -> None:
    payload = HelperPayload(
        operation_type="process",
        action_kind="shell.exec",
        run_mode="trusted",
        session_id="s1",
        cwd=str(tmp_path),
        env={},
        policy={"wallTimeoutS": 5.0},
        process=ProcessHelperPayload(argv=[sys.executable, "-c", "print('ok')"]),
        filesystem=None,
    )
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(encode_payload(payload), encoding="utf-8")

    exit_code = linux_helper.main(["--inner", "--payload", str(payload_path)])

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["returncode"] == 0
    assert result["stdout"] == "ok\n"


def test_build_outer_bwrap_command_reenters_helper_inner_mode(tmp_path) -> None:
    payload = HelperPayload(
        operation_type="process",
        action_kind="shell.exec",
        run_mode="trusted",
        session_id="s1",
        cwd=str(tmp_path),
        env={},
        policy={
            "network": "none",
            "mounts": [
                {"host": str(tmp_path), "sandbox": "/workspace", "mode": "rw", "required": True}
            ],
            "envAllowlist": ["PATH"],
            "tmpWritable": True,
            "wallTimeoutS": 5.0,
        },
        process=ProcessHelperPayload(argv=["/bin/echo", "ok"]),
        filesystem=None,
    )
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(encode_payload(payload), encoding="utf-8")

    argv = build_outer_bwrap_command(
        payload=payload,
        payload_path=payload_path,
        bwrap_path="bwrap",
        mount_proc=True,
    )

    assert argv[0] == "bwrap"
    assert "--unshare-user" in argv
    assert "--unshare-pid" in argv
    assert "--unshare-net" in argv
    separator = argv.index("--")
    inner = argv[separator + 1 :]
    assert inner[:3] == [sys.executable, "-m", "opensquilla.sandbox.backend.linux_helper"]
    assert "--inner" in inner
    assert str(payload_path) in inner


def test_build_outer_bwrap_command_wraps_proxy_allowlist_with_bridge(tmp_path) -> None:
    bridge_dir = tmp_path / "bridge"
    payload = HelperPayload(
        operation_type="process",
        action_kind="network.http",
        run_mode="trusted",
        session_id="s1",
        cwd=str(tmp_path),
        env={},
        policy={
            "network": "proxy_allowlist",
            "mounts": [
                {"host": str(tmp_path), "sandbox": "/workspace", "mode": "rw", "required": True}
            ],
            "envAllowlist": ["PATH"],
            "tmpWritable": True,
            "wallTimeoutS": 5.0,
            "linuxProxyBridge": {
                "udsPath": str(bridge_dir / "proxy.sock"),
                "scriptPath": str(bridge_dir / "inner_bridge.py"),
                "port": 18080,
            },
        },
        process=ProcessHelperPayload(argv=["/bin/echo", "ok"]),
        filesystem=None,
    )
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(encode_payload(payload), encoding="utf-8")

    argv = build_outer_bwrap_command(
        payload=payload,
        payload_path=payload_path,
        bwrap_path="bwrap",
        mount_proc=True,
    )

    separator = argv.index("--")
    inner = argv[separator + 1 :]
    assert "--unshare-net" in argv
    assert str(bridge_dir) in argv
    assert "OPENSQUILLA_SANDBOX_PROXY_UDS" in argv
    assert str(bridge_dir / "proxy.sock") in argv
    assert "OPENSQUILLA_SANDBOX_PROXY_PORT" in argv
    assert "18080" in argv
    assert "OPENSQUILLA_SANDBOX_EXEC_WRAPPER" not in argv
    assert "OPENSQUILLA_SANDBOX_POLICY_B64" not in argv
    assert inner[:3] == [sys.executable, str(bridge_dir / "inner_bridge.py"), "--"]
    assert inner[3:6] == [sys.executable, "-m", "opensquilla.sandbox.backend.linux_helper"]
    assert "--inner" in inner


def test_linux_helper_outer_mode_runs_bwrap_and_prints_inner_json(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    payload = HelperPayload(
        operation_type="process",
        action_kind="shell.exec",
        run_mode="trusted",
        session_id="s1",
        cwd=str(tmp_path),
        env={},
        policy={
            "network": "none",
            "mounts": [
                {"host": str(tmp_path), "sandbox": "/workspace", "mode": "rw", "required": True}
            ],
            "envAllowlist": ["PATH"],
            "tmpWritable": True,
            "wallTimeoutS": 5.0,
        },
        process=ProcessHelperPayload(argv=["/bin/echo", "ok"]),
        filesystem=None,
    )
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(encode_payload(payload), encoding="utf-8")

    class _Proc:
        returncode = 0

        async def communicate(self):
            payload = {
                "returncode": 0,
                "stdout": "ok\n",
                "stderr": "",
                "wallTimeS": 0.01,
                "timedOut": False,
                "truncatedStdout": False,
                "truncatedStderr": False,
            }
            return (
                json.dumps(payload).encode(),
                b"",
            )

    async def fake_create_subprocess_exec(*argv, **kwargs):
        assert argv[0] == "bwrap"
        assert "--unshare-user" in argv
        assert "--" in argv
        return _Proc()

    monkeypatch.setattr(
        linux_helper,
        "probe_bwrap",
        lambda: SimpleNamespace(
            available=True,
            message="ready",
            path="bwrap",
            ),
    )
    monkeypatch.setattr(
        linux_helper.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    exit_code = linux_helper.main(["--payload", str(payload_path)])

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["returncode"] == 0


def test_linux_helper_outer_mode_retries_without_proc_when_proc_mount_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    payload = HelperPayload(
        operation_type="process",
        action_kind="shell.exec",
        run_mode="trusted",
        session_id="s1",
        cwd=str(tmp_path),
        env={},
        policy={
            "network": "none",
            "mounts": [
                {"host": str(tmp_path), "sandbox": "/workspace", "mode": "rw", "required": True}
            ],
            "envAllowlist": ["PATH"],
            "tmpWritable": True,
            "wallTimeoutS": 5.0,
        },
        process=ProcessHelperPayload(argv=["/bin/echo", "ok"]),
        filesystem=None,
    )
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(encode_payload(payload), encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    class _Proc:
        def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self):
            return self._stdout, self._stderr

    async def fake_create_subprocess_exec(*argv, **kwargs):
        calls.append(tuple(argv))
        if len(calls) == 1:
            assert "--proc" in argv
            return _Proc(
                1,
                b"",
                b"bwrap: setting up /proc: Operation not permitted\n",
            )
        assert "--proc" not in argv
        return _Proc(
            0,
            json.dumps(
                {
                    "returncode": 0,
                    "stdout": "ok\n",
                    "stderr": "",
                    "wallTimeS": 0.01,
                    "timedOut": False,
                    "truncatedStdout": False,
                    "truncatedStderr": False,
                }
            ).encode(),
            b"",
        )

    monkeypatch.setattr(
        linux_helper,
        "probe_bwrap",
        lambda: SimpleNamespace(
            available=True,
            message="ready",
            path="bwrap",
        ),
    )
    monkeypatch.setattr(
        linux_helper.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    exit_code = linux_helper.main(["--payload", str(payload_path)])

    assert exit_code == 0
    assert len(calls) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["returncode"] == 0
    assert result["stdout"] == "ok\n"
    assert result["stdout"] == "ok\n"


def test_linux_helper_outer_mode_passes_preserved_fds_for_denied_globs(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    secret = tmp_path / ".env"
    secret.write_text("secret", encoding="utf-8")
    captured: dict[str, object] = {}
    payload = HelperPayload(
        operation_type="process",
        action_kind="shell.exec",
        run_mode="trusted",
        session_id="s1",
        cwd=str(tmp_path),
        env={},
        policy={
            "network": "none",
            "mounts": [
                {"host": str(tmp_path), "sandbox": "/workspace", "mode": "ro", "required": True}
            ],
            "unreadableGlobs": [str(tmp_path / "**" / ".env")],
            "envAllowlist": ["PATH"],
            "tmpWritable": True,
            "wallTimeoutS": 5.0,
        },
        process=ProcessHelperPayload(argv=["/bin/echo", "ok"]),
        filesystem=None,
    )
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(encode_payload(payload), encoding="utf-8")

    class _Proc:
        returncode = 0

        async def communicate(self):
            payload = {
                "returncode": 0,
                "stdout": "ok\n",
                "stderr": "",
                "wallTimeS": 0.01,
                "timedOut": False,
                "truncatedStdout": False,
                "truncatedStderr": False,
            }
            return (
                json.dumps(payload).encode(),
                b"",
            )

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(
        linux_helper,
        "probe_bwrap",
        lambda: SimpleNamespace(
            available=True,
            message="ready",
            path="bwrap",
        ),
    )
    monkeypatch.setattr(
        linux_helper.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    exit_code = linux_helper.main(["--payload", str(payload_path)])

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["returncode"] == 0
    kwargs = captured["kwargs"]
    assert kwargs["pass_fds"]
    argv = captured["argv"]
    assert "--ro-bind" in argv
    assert str(secret) in argv


def test_linux_helper_outer_mode_reports_protected_metadata_creation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    parent_git = tmp_path / ".git"
    parent_git.mkdir()
    (parent_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    workspace = tmp_path / "child"
    workspace.mkdir()
    protected = workspace / ".git"
    payload = HelperPayload(
        operation_type="process",
        action_kind="shell.exec",
        run_mode="trusted",
        session_id="s1",
        cwd=str(workspace),
        env={},
        policy={
            "network": "none",
            "mounts": [
                {
                    "host": str(workspace),
                    "sandbox": "/workspace",
                    "mode": "rw",
                    "required": True,
                }
            ],
            "envAllowlist": ["PATH"],
            "tmpWritable": True,
            "wallTimeoutS": 5.0,
        },
        process=ProcessHelperPayload(argv=["/bin/true"]),
        filesystem=None,
    )
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(encode_payload(payload), encoding="utf-8")

    class _Proc:
        returncode = 0

        async def communicate(self):
            protected.mkdir()
            payload = {
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "wallTimeS": 0.01,
                "timedOut": False,
                "truncatedStdout": False,
                "truncatedStderr": False,
            }
            return (
                json.dumps(payload).encode(),
                b"",
            )

    async def fake_create_subprocess_exec(*argv, **kwargs):
        return _Proc()

    monkeypatch.setattr(
        linux_helper,
        "probe_bwrap",
        lambda: SimpleNamespace(
            available=True,
            message="ready",
            path="bwrap",
        ),
    )
    monkeypatch.setattr(
        linux_helper.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    exit_code = linux_helper.main(["--payload", str(payload_path)])

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["returncode"] == 1
    assert "sandbox blocked creation of protected workspace metadata path" in result["stderr"]
    assert str(protected) in result["stderr"]
    assert not protected.exists()


def test_linux_helper_outer_mode_cleans_synthetic_metadata_mount_target(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    protected = tmp_path / ".codex"
    payload = HelperPayload(
        operation_type="process",
        action_kind="shell.exec",
        run_mode="trusted",
        session_id="s1",
        cwd=str(tmp_path),
        env={},
        policy={
            "network": "none",
            "mounts": [
                {"host": str(tmp_path), "sandbox": "/workspace", "mode": "rw", "required": True}
            ],
            "envAllowlist": ["PATH"],
            "tmpWritable": True,
            "wallTimeoutS": 5.0,
        },
        process=ProcessHelperPayload(argv=["/bin/true"]),
        filesystem=None,
    )
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(encode_payload(payload), encoding="utf-8")

    class _Proc:
        returncode = 0

        async def communicate(self):
            protected.mkdir()
            payload = {
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "wallTimeS": 0.01,
                "timedOut": False,
                "truncatedStdout": False,
                "truncatedStderr": False,
            }
            return (
                json.dumps(payload).encode(),
                b"",
            )

    async def fake_create_subprocess_exec(*argv, **kwargs):
        return _Proc()

    monkeypatch.setattr(
        linux_helper,
        "probe_bwrap",
        lambda: SimpleNamespace(
            available=True,
            message="ready",
            path="bwrap",
        ),
    )
    monkeypatch.setattr(
        linux_helper.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    exit_code = linux_helper.main(["--payload", str(payload_path)])

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["returncode"] == 0
    assert result["stderr"] == ""
    assert not protected.exists()


def test_synthetic_mount_cleanup_preserves_non_empty_directory(tmp_path) -> None:
    protected = tmp_path / ".codex"
    protected.mkdir()
    (protected / "config.json").write_text("{}", encoding="utf-8")

    cleanup_synthetic_mount_targets(
        (SyntheticMountCleanupTarget(path=protected, kind="empty_directory"),)
    )

    assert protected.exists()
    assert (protected / "config.json").read_text(encoding="utf-8") == "{}"


def test_synthetic_mount_cleanup_preserves_pre_existing_empty_file(tmp_path) -> None:
    protected = tmp_path / ".git"
    protected.write_text("", encoding="utf-8")
    identity = SyntheticMountCleanupTarget.identity_for_path(protected)
    assert identity is not None

    cleanup_synthetic_mount_targets(
        (
            SyntheticMountCleanupTarget(
                path=protected,
                kind="empty_file",
                pre_existing_identity=identity,
            ),
        )
    )

    assert protected.exists()


def test_synthetic_mount_registry_waits_for_other_active_registration(tmp_path) -> None:
    protected = tmp_path / ".codex"
    protected.mkdir()
    target = SyntheticMountCleanupTarget(path=protected, kind="empty_directory")
    first = register_synthetic_mount_targets((target,))
    second = register_synthetic_mount_targets((target,))

    cleanup_synthetic_mount_registrations(first)

    assert protected.exists()

    cleanup_synthetic_mount_registrations(second)

    assert not protected.exists()


def test_protected_create_registry_waits_before_removing_created_path(tmp_path) -> None:
    protected = tmp_path / ".git"
    first = register_protected_create_targets((protected,))
    second = register_protected_create_targets((protected,))
    protected.mkdir()

    first_messages = cleanup_protected_create_registrations(first)

    assert first_messages == [
        f"sandbox blocked creation of protected workspace metadata path {protected}"
    ]
    assert protected.exists()

    second_messages = cleanup_protected_create_registrations(second)

    assert second_messages == [
        f"sandbox blocked creation of protected workspace metadata path {protected}"
    ]
    assert not protected.exists()
