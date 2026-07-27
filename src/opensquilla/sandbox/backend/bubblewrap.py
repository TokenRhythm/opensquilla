"""Linux bubblewrap backend.

Materializes helper payloads and supervises a helper subprocess for Linux
process and filesystem operations. The helper's outer stage plans and invokes
``bwrap``; the inner stage runs the requested process or filesystem worker.

Design notes:

* The sandbox layout is a minimal root view: ``/`` starts as tmpfs, host
  runtime directories such as ``/usr`` and ``/lib`` are mounted read-only,
  ``/dev`` receives a curated dev node set, ``/proc`` is a fresh proc mount,
  ``/tmp`` is a tmpfs when the policy allows it, and policy mounts are
  canonicalized before they reach the Linux planner.
* Network namespaces are unshared whenever the policy selects
  ``NetworkMode.NONE``. For ``NetworkMode.HOST`` we deliberately keep the
  host network visible (no ``--unshare-net``) — this is only reached when
  the policy layer has opted in (e.g. ``STANDARD`` + network-tagged
  action).
* Capabilities are dropped via ``--cap-drop ALL`` and ``--new-session``
  detaches the controlling terminal to prevent ``TIOCSTI`` style escapes.
* Process output is captured in memory with an upper bound; excess bytes are
  truncated and the ``truncated_*`` flags are set on the result.
* Wall timeouts are enforced inside the helper; on expiry we
  terminate the process group so dangling grandchildren do not outlive the
  sandbox. If SIGTERM isn't observed within a small grace window we follow
  with SIGKILL.

The backend does not assume root and never tries to privilege-up. If the
kernel forbids user namespaces the ``bwrap`` invocation will surface a
non-zero exit which we translate into a :class:`SandboxBackendError`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from opensquilla.sandbox.backend.base import Backend
from opensquilla.sandbox.backend.filesystem_worker_policy import (
    build_filesystem_worker_policy,
)
from opensquilla.sandbox.backend.linux_bwrap import (
    BwrapOptions,
    BwrapPlan,
)
from opensquilla.sandbox.backend.linux_bwrap import (
    build_bwrap_plan as build_linux_bwrap_plan,
)
from opensquilla.sandbox.backend.linux_paths import canonical_linux_policy
from opensquilla.sandbox.backend.linux_payload import (
    HelperPayload,
    build_filesystem_helper_payload,
    build_process_helper_payload,
    encode_payload,
    encode_policy_b64,
)
from opensquilla.sandbox.backend.linux_permissions import compile_linux_permissions
from opensquilla.sandbox.backend.linux_proxy_bridge import (
    ENV_EXEC_WRAPPER,
    ENV_POLICY_B64,
    ENV_PROXY_PORT,
    ENV_PROXY_UDS,
    LinuxProxyBridgeHost,
)
from opensquilla.sandbox.backend.linux_proxy_routing import proxy_env_for_inner_port
from opensquilla.sandbox.backend.linux_readiness import probe_bwrap
from opensquilla.sandbox.operation_runtime import (
    FilesystemOperationRequest,
    SandboxOperation,
    SandboxOperationDomain,
    SandboxOperationResult,
)
from opensquilla.sandbox.types import (
    MountSpec,
    NetworkMode,
    SandboxBackendError,
    SandboxPolicy,
    SandboxRequest,
    SandboxResult,
)

log = logging.getLogger(__name__)

_BWRAP_BINARY = "bwrap"
_OUTPUT_BYTE_CAP = 1_048_576  # 1 MiB per stream
_TERMINATE_GRACE_S = 2.0
_HELPER_TIMEOUT_GRACE_S = _TERMINATE_GRACE_S + 1.0
_DEFAULT_BRIDGE_UDS_PATH = Path("/tmp/opensquilla-sandbox-proxy.sock")
_DEFAULT_BRIDGE_SCRIPT_NAME = "inner_bridge.py"
_BWRAP_PYTHON_CANDIDATES: tuple[Path, ...] = (
    Path("/usr/bin/python3"),
    Path("/bin/python3"),
    Path("/usr/bin/python"),
    Path("/bin/python"),
)
def _bridge_python_path() -> Path:
    for candidate in _BWRAP_PYTHON_CANDIDATES:
        if candidate.exists():
            return candidate
    raise SandboxBackendError(
        "NetworkMode.PROXY_ALLOWLIST requires a system Python mounted "
        "inside the bubblewrap sandbox"
    )


def _default_bridge_script_path(uds_path: Path) -> Path:
    return uds_path.parent / _DEFAULT_BRIDGE_SCRIPT_NAME


def _proxy_bridge_child_argv(
    request: SandboxRequest,
    *,
    bridge_script_path: Path,
) -> list[str]:
    return [
        str(_bridge_python_path()),
        str(bridge_script_path),
        "--",
        *request.argv,
    ]


def materialize_linux_exec_wrapper(path: Path) -> None:
    source = Path(__file__).with_name("linux_exec_wrapper.py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def build_bwrap_argv(
    request: SandboxRequest,
    *,
    binary: str = _BWRAP_BINARY,
    bridge_uds_path: Path | None = None,
    bridge_script_path: Path | None = None,
    bridge_port: int | None = None,
    exec_wrapper_path: Path | None = None,
    mount_proc: bool = True,
) -> list[str]:
    return build_bwrap_plan(
        request,
        binary=binary,
        bridge_uds_path=bridge_uds_path,
        bridge_script_path=bridge_script_path,
        bridge_port=bridge_port,
        exec_wrapper_path=exec_wrapper_path,
        mount_proc=mount_proc,
    ).argv


def build_bwrap_plan(
    request: SandboxRequest,
    *,
    binary: str = _BWRAP_BINARY,
    bridge_uds_path: Path | None = None,
    bridge_script_path: Path | None = None,
    bridge_port: int | None = None,
    exec_wrapper_path: Path | None = None,
    mount_proc: bool = True,
) -> BwrapPlan:
    """Return the ``bwrap`` argv for direct/background process execution."""
    policy = canonical_linux_policy(request.policy)
    env = _direct_bwrap_env(policy, request.env)
    command = list(request.argv)
    if policy.network == NetworkMode.PROXY_ALLOWLIST:
        if policy.network_proxy is None:
            raise SandboxBackendError(
                "NetworkMode.PROXY_ALLOWLIST requires a network proxy "
                "for the bubblewrap backend"
            )
        bridge_uds_path = bridge_uds_path or _DEFAULT_BRIDGE_UDS_PATH
        bridge_script_path = bridge_script_path or _default_bridge_script_path(
            bridge_uds_path,
        )
        bridge_port = bridge_port or policy.network_proxy.port
        if bridge_port <= 0 or bridge_port > 65535:
            raise SandboxBackendError(
                "NetworkMode.PROXY_ALLOWLIST requires a valid proxy port "
                "for the bubblewrap backend"
            )
        if bridge_script_path.parent != bridge_uds_path.parent:
            raise SandboxBackendError(
                "linux proxy bridge script must be in the mounted bridge directory"
            )
        policy = _policy_with_bridge_mount(policy, bridge_uds_path.parent)
        env.update(
            proxy_env_for_inner_port(
                base_env={
                    ENV_PROXY_UDS: str(bridge_uds_path),
                    ENV_PROXY_PORT: str(bridge_port),
                    ENV_POLICY_B64: encode_policy_b64(policy),
                    **({ENV_EXEC_WRAPPER: str(exec_wrapper_path)} if exec_wrapper_path else {}),
                },
                port=bridge_port,
            )
        )
        command = _proxy_bridge_child_argv(
            request,
            bridge_script_path=bridge_script_path,
        )
    elif exec_wrapper_path is not None:
        policy = _policy_with_exec_wrapper_mount(policy, exec_wrapper_path)
        command = _exec_wrapper_child_argv(
            request,
            exec_wrapper_path=exec_wrapper_path,
            policy=policy,
        )

    return build_linux_bwrap_plan(
        command=command,
        command_cwd=request.cwd,
        permissions=compile_linux_permissions(policy),
        options=BwrapOptions(
            bwrap_path=binary,
            mount_proc=mount_proc,
            env=env,
        ),
    )


def _direct_bwrap_env(
    policy: SandboxPolicy,
    override_env: dict[str, str],
) -> dict[str, str]:
    allowlist = set(policy.env_allowlist)
    env: dict[str, str] = {}
    for key in policy.env_allowlist:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    for key, value in override_env.items():
        if key in allowlist:
            env[key] = value
    return env


def _policy_with_bridge_mount(policy: SandboxPolicy, bridge_dir: Path) -> SandboxPolicy:
    bridge_mount = MountSpec(
        host_path=bridge_dir,
        sandbox_path=bridge_dir,
        mode="rw",
        required=True,
    )
    return replace(policy, mounts=(*policy.mounts, bridge_mount))


def _policy_with_exec_wrapper_mount(
    policy: SandboxPolicy,
    exec_wrapper_path: Path,
) -> SandboxPolicy:
    wrapper_mount = MountSpec(
        host_path=exec_wrapper_path.parent,
        sandbox_path=exec_wrapper_path.parent,
        mode="ro",
        required=True,
    )
    return replace(policy, mounts=(*policy.mounts, wrapper_mount))


def _exec_wrapper_child_argv(
    request: SandboxRequest,
    *,
    exec_wrapper_path: Path,
    policy: SandboxPolicy,
) -> list[str]:
    return [
        str(_bridge_python_path()),
        str(exec_wrapper_path),
        "--policy-b64",
        encode_policy_b64(policy),
        "--",
        *request.argv,
    ]


class BubblewrapBackend(Backend):
    """Linux bubblewrap-backed implementation of :class:`Backend`."""

    name = "bubblewrap"

    def __init__(self, binary: str = _BWRAP_BINARY) -> None:
        self._binary = binary

    def available(self) -> bool:
        if not sys.platform.startswith("linux"):
            return False
        return probe_bwrap().available

    def operation_domains_supported(self) -> frozenset[SandboxOperationDomain]:
        return frozenset({"filesystem"})

    async def run_operation(self, operation: SandboxOperation) -> SandboxOperationResult:
        if operation.domain != "filesystem":
            raise SandboxBackendError(
                f"bubblewrap backend does not implement {operation.domain} operations"
            )
        if not isinstance(operation.request, FilesystemOperationRequest):
            raise SandboxBackendError("filesystem operation is missing filesystem request")
        if operation.workspace is None:
            raise SandboxBackendError("filesystem operation is missing workspace")
        worker_root = operation.workspace / ".opensquilla-cache" / "fs-worker"
        worker_root.mkdir(parents=True, exist_ok=True)
        payload_path = worker_root / f"{time.monotonic_ns()}.json"
        policy = build_filesystem_worker_policy(
            operation,
            private_rw_roots=(payload_path.parent,),
            private_ro_roots=(),
            env_allowlist=("PATH", "PYTHONPATH", "HOME", "TMP", "TEMP"),
            description=f"Linux filesystem worker policy for {operation.kind}",
        )
        helper_payload = build_filesystem_helper_payload(
            operation,
            policy=policy,
            session_id="",
            worker_payload_path=payload_path,
        )
        result = await _run_linux_helper_payload(helper_payload)
        message = result.get("message")
        if not isinstance(message, str):
            raise SandboxBackendError("linux filesystem worker returned invalid result")
        return SandboxOperationResult(
            message=message,
            created=bool(result.get("created", False)),
        )

    async def run(self, request: SandboxRequest) -> SandboxResult:
        probe = probe_bwrap()
        if not probe.available:
            raise SandboxBackendError(f"bubblewrap backend unavailable: {probe.message}")
        helper_payload = build_process_helper_payload(request)
        if request.policy.network == NetworkMode.PROXY_ALLOWLIST:
            if request.policy.network_proxy is None:
                raise SandboxBackendError(
                    "NetworkMode.PROXY_ALLOWLIST requires a network proxy "
                    "for the bubblewrap backend"
                )
            with tempfile.TemporaryDirectory(prefix="opensquilla-linux-proxy-") as temp_dir:
                bridge = LinuxProxyBridgeHost(
                    Path(temp_dir) / "proxy.sock",
                    request.policy.network_proxy.host,
                    request.policy.network_proxy.port,
                )
                await bridge.start()
                try:
                    result = await _run_linux_helper_payload(
                        _with_linux_proxy_bridge(helper_payload, bridge)
                    )
                finally:
                    await bridge.stop()
        else:
            result = await _run_linux_helper_payload(helper_payload)
        return SandboxResult(
            returncode=_int_result(result.get("returncode"), default=-1),
            stdout=str(result.get("stdout", "")),
            stderr=str(result.get("stderr", "")),
            wall_time_s=_float_result(result.get("wallTimeS"), default=0.0),
            backend_used=self.name,
            policy_used=request.policy.summary(),
            truncated_stdout=bool(result.get("truncatedStdout", False)),
            truncated_stderr=bool(result.get("truncatedStderr", False)),
            timed_out=bool(result.get("timedOut", False)),
        )


def _decode_capped(raw: bytes | None) -> tuple[str, bool]:
    if not raw:
        return "", False
    if len(raw) <= _OUTPUT_BYTE_CAP:
        return raw.decode("utf-8", errors="replace"), False
    return raw[:_OUTPUT_BYTE_CAP].decode("utf-8", errors="replace"), True


def _int_result(value: object, *, default: int) -> int:
    if isinstance(value, (str, bytes, int, float)):
        return int(value)
    return default


def _float_result(value: object, *, default: float) -> float:
    if isinstance(value, (str, bytes, int, float)):
        return float(value)
    return default


async def _terminate_process_group(
    proc: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    """Best-effort process-group cleanup after a wall-timeout.

    We can't ``communicate`` a second time once the outer ``wait_for`` was
    cancelled, so we signal the group and then gather whatever the transport
    buffered. If SIGTERM doesn't clear the group we escalate to SIGKILL.
    """
    pid = proc.pid
    os_mod = cast(Any, os)
    signal_mod = cast(Any, signal)
    try:
        os_mod.killpg(pid, signal_mod.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_S)
    except TimeoutError:
        try:
            os_mod.killpg(pid, signal_mod.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await proc.wait()
        except ProcessLookupError:
            pass

    stdout = b""
    stderr = b""
    if proc.stdout is not None:
        try:
            stdout = await proc.stdout.read()
        except Exception:  # noqa: BLE001 — best effort after kill
            stdout = b""
    if proc.stderr is not None:
        try:
            stderr = await proc.stderr.read()
        except Exception:  # noqa: BLE001 — best effort after kill
            stderr = b""
    return stdout, stderr


async def _run_linux_helper_payload(payload: HelperPayload) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="opensquilla-linux-helper-") as temp_dir:
        payload_path = Path(temp_dir) / "payload.json"
        payload_path.write_text(encode_payload(payload), encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "opensquilla.sandbox.backend.linux_helper",
            "--payload",
            str(payload_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(
                proc.communicate(),
                timeout=_outer_helper_timeout_s(payload),
            )
        except TimeoutError as exc:
            await _terminate_process_group(proc)
            raise SandboxBackendError("linux helper timed out") from exc
    stdout = stdout_raw.decode("utf-8", errors="replace")
    stderr = stderr_raw.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise SandboxBackendError(stderr.strip() or stdout.strip() or "linux helper failed")
    result = json.loads(stdout)
    if not isinstance(result, dict):
        raise SandboxBackendError("linux helper returned invalid result")
    return result


def _outer_helper_timeout_s(payload: HelperPayload) -> float:
    try:
        wall_timeout = float(payload.policy.get("wallTimeoutS", 60.0))
    except (TypeError, ValueError):
        wall_timeout = 60.0
    return max(0.01, wall_timeout) + _HELPER_TIMEOUT_GRACE_S


def _with_linux_proxy_bridge(
    payload: HelperPayload,
    bridge: LinuxProxyBridgeHost,
) -> HelperPayload:
    policy = dict(payload.policy)
    policy["linuxProxyBridge"] = {
        "udsPath": str(bridge.uds_path),
        "scriptPath": str(bridge.script_path),
        "port": bridge.upstream_port,
    }
    return replace(payload, policy=policy)


__all__ = [
    "BubblewrapBackend",
    "LinuxProxyBridgeHost",
    "build_bwrap_argv",
    "build_bwrap_plan",
]
