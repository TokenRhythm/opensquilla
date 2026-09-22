"""Offline release gate, never run by application startup.

Exercise the shipped Safe backend and frozen internal children. Windows setup
is permitted only by an explicit flag on a disposable CI runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace


async def verify_safe_execution() -> dict[str, object]:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.integration import configure_runtime, get_runtime, reset_runtime
    from opensquilla.sandbox.operation_runtime import SandboxOperation
    from opensquilla.sandbox.permissions import FileSystemPermissionProfile
    from opensquilla.sandbox.runtime_launcher import ChildRole, internal_child_argv
    from opensquilla.sandbox.setup_runtime import (
        current_sandbox_capability_report,
        initialize_sandbox_runtime,
        reset_sandbox_setup_runtime_state,
    )
    from opensquilla.sandbox.setup_state import SandboxSetupState, ensure_sandbox_setup
    from opensquilla.sandbox.types import (
        NetworkMode,
        ResourceLimits,
        SandboxPolicy,
        SandboxRequest,
        SecurityLevel,
    )

    root = Path(os.environ["OPENSQUILLA_STATE_DIR"]).resolve()
    workspace = root / "safe-execution"
    workspace.mkdir()
    target = workspace / "read-only.txt"
    target.write_text("synthetic Safe fixture\n", encoding="utf-8")
    config = SimpleNamespace(state_dir=str(root / "state"))
    if sys.platform == "win32" and os.environ.get("OPENSQUILLA_SMOKE_PROVISION_SANDBOX") == "1":
        import ctypes

        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise RuntimeError("CI sandbox provisioning requires an elevated disposable runner")
        setup = await ensure_sandbox_setup(config)
        if setup.state is not SandboxSetupState.READY:
            raise RuntimeError(f"CI sandbox provisioning failed: {setup.detail}")

    reset_sandbox_setup_runtime_state()
    configure_runtime(SandboxSettings(run_mode="safe"), workspace=workspace, defer_backend=True)
    try:
        setup = await initialize_sandbox_runtime(config)
        report = await current_sandbox_capability_report(config)
        if setup.state is not SandboxSetupState.READY or not report.available:
            raise RuntimeError(f"Safe initialization failed: {setup.detail}")
        backend = get_runtime().backend
        if backend.name not in {"seatbelt", "windows_default", "bubblewrap"}:
            raise RuntimeError(f"Expected real isolation, received {backend.name}")

        files = FileSystemPermissionProfile.workspace(
            workspace=workspace, host_root_readonly=False,
            tmp_writable=False, tmpdir_env_writable=False,
        )
        read = await backend.run_operation(SandboxOperation.filesystem(
            kind="read_file", path=target, workspace=workspace, run_mode="safe",
            file_system_profile=files,
        ))
        if "synthetic Safe fixture" not in read.message:
            raise RuntimeError("Safe filesystem worker did not read the fixture")
        output = workspace / "generated.html"
        await backend.run_operation(SandboxOperation.filesystem(
            kind="write_text", path=output, content="<h1>Safe smoke</h1>",
            workspace=workspace, run_mode="safe", file_system_profile=files,
        ))
        if output.read_text(encoding="utf-8") != "<h1>Safe smoke</h1>":
            raise RuntimeError("Safe filesystem worker did not write its allowed output")

        policy = SandboxPolicy(
            level=SecurityLevel.STANDARD, network=NetworkMode.NONE, mounts=(),
            workspace_rw=False, tmp_writable=False, require_approval=False,
            limits=ResourceLimits(wall_timeout_s=15),
            env_allowlist=("PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "SystemRoot", "WINDIR"),
            file_system=FileSystemPermissionProfile.read_only(host_root_readonly=True),
        )

        async def execute(code: str) -> str:
            result = await backend.run(SandboxRequest(
                argv=internal_child_argv(ChildRole.PYTHON_CODE, args=(code,)),
                cwd=workspace, action_kind="code.exec", policy=policy, run_mode="safe",
                env={
                    key: value for key, value in os.environ.items() if key in policy.env_allowlist
                },
            ))
            if result.returncode != 0 or result.timed_out:
                raise RuntimeError(f"Safe internal child failed: {result.stderr}")
            return result.stdout.strip()

        if await execute('print("readonly-child-ok")') != "readonly-child-ok":
            raise RuntimeError("Read-only internal child did not execute")
        denied = await execute('''from pathlib import Path
try:
    Path("read-only.txt").write_text("forbidden", encoding="utf-8")
except PermissionError:
    print("write-denied")
else:
    raise RuntimeError("read-only policy allowed a write")
''')
        if (
            denied != "write-denied"
            or target.read_text(encoding="utf-8") != "synthetic Safe fixture\n"
        ):
            raise RuntimeError("Read-only policy did not preserve the fixture")

        # A real listening endpoint distinguishes isolation from connection refusal.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(4)
            address = listener.getsockname()
            with socket.create_connection(address, timeout=2):
                accepted, _ = listener.accept()
                accepted.close()
            blocked = await execute(f'''import socket
try:
    connection = socket.create_connection({address!r}, timeout=1)
except OSError:
    print("network-denied")
else:
    connection.close()
    raise RuntimeError("network=none allowed a connection")
''')
            if blocked != "network-denied":
                raise RuntimeError("Safe network boundary was not enforced")
        return {
            "probe": "opensquilla-desktop-safe-execution",
            "frozen": bool(getattr(sys, "frozen", False)),
            "backend": backend.name,
            "read": True, "write": True, "readonlyChild": True,
            "writeDenied": True, "networkDenied": True,
        }
    finally:
        reset_runtime()
        reset_sandbox_setup_runtime_state()


if __name__ == "__main__":
    with contextlib.redirect_stdout(sys.stderr):
        result = asyncio.run(verify_safe_execution())
    print(json.dumps(result))
