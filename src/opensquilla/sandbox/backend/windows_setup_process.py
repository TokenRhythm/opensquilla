"""Observe explicit Windows setup without keeping UAC in a Gateway thread.

Only an explicit setup request starts this short-lived process. The elevated
helper still validates its target, owns the system mutation lock, and writes
the existing setup marker. The pipe carries progress, never credentials.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult

LAUNCHER_ARGUMENT = "--windows-setup-launcher"
OPERATION_TIMEOUT_SECONDS = 300.0
CLEANUP_TIMEOUT_SECONDS = 2.0
_PROTOCOL_PREFIX = "opensquilla-setup:"
_MAX_OUTPUT_BYTES = 64 * 1024
_OWNER_FIELDS = ("ownerPid", "ownerCreated", "launcherPid", "launcherCreated")
_OPERATION_OWNER: dict[str, str] = {}
_UNCONFIRMED_LAUNCHER: asyncio.subprocess.Process | None = None


def _process_identity(pid: int) -> str | None:
    # Reuse the native creation-time probe: a recycled PID cannot authorize a
    # delayed UAC callback. This runs only on explicit setup/checkpoints.
    import ctypes
    from ctypes import wintypes

    from opensquilla.gateway.desktop_ownership import _windows_process_start_identity

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        return _windows_process_start_identity(pid)
    finally:
        kernel32.CloseHandle(handle)


def enrich_setup_payload(payload: dict[str, str]) -> dict[str, str]:
    """Fence an elevated request to its exact caller and launcher processes."""
    owner = dict(_OPERATION_OWNER)
    if not owner:
        identity = _process_identity(os.getpid())
        if identity is None:
            raise OSError("windows_setup_owner_identity_unavailable")
        owner = {"ownerPid": str(os.getpid()), "ownerCreated": identity}
    if "launcherPid" not in owner:
        identity = _process_identity(os.getpid())
        if identity is None:
            raise OSError("windows_setup_launcher_identity_unavailable")
        owner.update(launcherPid=str(os.getpid()), launcherCreated=identity)
    return {**payload, **owner}


def require_setup_owner(payload: Mapping[str, str]) -> None:
    """Reject a delayed helper after the request's launcher or Gateway exits.

    Legacy internal callers without an owner remain valid. Once any fencing
    field is supplied, the complete pair is required and failure is closed.
    """
    if not any(key in payload for key in _OWNER_FIELDS):
        return
    for pid_key, created_key in (
        ("ownerPid", "ownerCreated"),
        ("launcherPid", "launcherCreated"),
    ):
        try:
            pid = int(payload[pid_key])
            expected = payload[created_key]
        except (KeyError, TypeError, ValueError) as exc:
            raise OSError("windows_setup_owner_invalid") from exc
        if pid <= 0 or not expected or _process_identity(pid) != expected:
            raise OSError("windows_setup_owner_retired")


def _launcher_command(owner: dict[str, str]) -> list[str]:
    encoded = base64.urlsafe_b64encode(json.dumps(owner).encode("utf-8")).decode("ascii")
    if getattr(sys, "frozen", False):
        return [sys.executable, LAUNCHER_ARGUMENT, encoded]
    return [sys.executable, "-m", __name__, LAUNCHER_ARGUMENT, encoded]


def _failed(detail: str) -> SetupResult:
    return SetupResult(
        state=SandboxSetupState.FAILED,
        platform="win32",
        message="Windows sandbox preparation did not complete.",
        requires_admin=True,
        detail=detail,
    )


async def _stop_launcher(process: asyncio.subprocess.Process) -> bool:
    """Stop only our observer; this does not assert elevated work was undone."""
    if process.returncode is None:
        try:
            process.kill()
        except (ProcessLookupError, PermissionError):
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=CLEANUP_TIMEOUT_SECONDS)
    except (TimeoutError, ProcessLookupError):
        pass
    return process.returncode is not None


async def _observe_launcher(
    process: asyncio.subprocess.Process,
    on_unavailable: Callable[[str], None] | None,
) -> SetupResult:
    assert process.stdout is not None
    total = 0
    result: SetupResult | None = None
    while line := await process.stdout.readline():
        total += len(line)
        if total > _MAX_OUTPUT_BYTES:
            raise ValueError("windows_setup_launcher_output_limit")
        text = line.decode("utf-8", errors="replace").rstrip()
        if not text.startswith(_PROTOCOL_PREFIX):
            continue
        event = json.loads(text[len(_PROTOCOL_PREFIX) :])
        if not isinstance(event, dict):
            raise ValueError("windows_setup_launcher_invalid_event")
        if event.get("event") == "unavailable":
            if on_unavailable is not None:
                on_unavailable(str(event.get("detail", ""))[:4096])
        elif event.get("event") == "result":
            if result is not None:
                raise ValueError("windows_setup_launcher_duplicate_result")
            payload = event["result"]
            result = SetupResult(
                state=SandboxSetupState(payload["state"]),
                platform="win32",
                message=str(payload["message"])[:1024],
                requires_admin=bool(payload.get("requiresAdmin", False)),
                detail=str(payload["detail"])[:4096] if payload.get("detail") else None,
            )
    exit_code = await process.wait()
    if result is not None and result.state is SandboxSetupState.FAILED:
        return result
    if exit_code != 0 or result is None:
        return _failed(
            f"windows_setup_launcher_failed: exit={exit_code}; result_missing={result is None}"
        )
    return result


async def run_windows_setup_process(
    config: Any,
    *,
    on_unavailable: Callable[[str], None] | None = None,
    repair_identity: bool = False,
) -> SetupResult:
    """Run explicit setup out of process for both normal and admin callers."""
    global _UNCONFIRMED_LAUNCHER

    _ = config  # Setup uses the selected profile from the inherited environment.
    if _UNCONFIRMED_LAUNCHER is not None:
        if _UNCONFIRMED_LAUNCHER.returncode is None:
            return _failed(
                "windows_setup_previous_operation_unconfirmed: preparation_still_running"
            )
        _UNCONFIRMED_LAUNCHER = None
    identity = _process_identity(os.getpid())
    if identity is None:
        return _failed("windows_setup_owner_identity_unavailable")
    owner = {"ownerPid": str(os.getpid()), "ownerCreated": identity}
    if repair_identity:
        owner["repairIdentity"] = "true"
    options: dict[str, Any] = {}
    if sys.platform.startswith("win"):
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        process = await asyncio.create_subprocess_exec(
            *_launcher_command(owner),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=8192,
            **options,
        )
    except OSError as exc:
        return _failed(f"windows_setup_launcher_spawn_failed: {exc}")
    try:
        async with asyncio.timeout(OPERATION_TIMEOUT_SECONDS):
            result = await _observe_launcher(process, on_unavailable)
    except TimeoutError:
        result = _failed("windows_setup_operation_timeout: elevated_completion_unconfirmed")
    except (ValueError, KeyError, TypeError) as exc:
        result = _failed(f"windows_setup_launcher_protocol_failed: {type(exc).__name__}")
    finally:
        try:
            confirmed = await _stop_launcher(process)
        finally:
            if process.returncode is None:
                # Even a second shutdown cancellation must retain the handle.
                # Retry becomes possible when its actual exit is observed.
                _UNCONFIRMED_LAUNCHER = process
    if not confirmed:
        return _failed("windows_setup_launcher_exit_unconfirmed: elevated_completion_unconfirmed")
    return result


def _emit(event: dict[str, object]) -> None:
    print(_PROTOCOL_PREFIX + json.dumps(event, separators=(",", ":")), flush=True)


def _watch_owner(stop: threading.Event) -> None:
    """Contain a stuck consent dialog even if the Gateway itself crashes."""
    deadline = time.monotonic() + OPERATION_TIMEOUT_SECONDS
    while not stop.wait(1.0):
        try:
            require_setup_owner(_OPERATION_OWNER)
            if time.monotonic() >= deadline:
                raise OSError("windows_setup_operation_timeout")
        except OSError:
            # Only this disposable launcher exits. The elevated helper checks
            # the launcher's identity before further system mutations.
            os._exit(125)


def setup_launcher_main(argv: list[str] | None = None) -> int:
    global _OPERATION_OWNER
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != LAUNCHER_ARGUMENT:
        return 2
    stop = threading.Event()
    try:
        raw = json.loads(base64.urlsafe_b64decode(args[1]).decode("utf-8"))
        if (
            not isinstance(raw, dict)
            or not {"ownerPid", "ownerCreated"} <= raw.keys()
            or raw.keys() - {"ownerPid", "ownerCreated", "repairIdentity"}
            or raw.get("repairIdentity", "false") not in {"true", "false"}
        ):
            return 2
        repair_identity = raw.pop("repairIdentity", "false") == "true"
        _OPERATION_OWNER = raw
        _OPERATION_OWNER = enrich_setup_payload(raw)
        require_setup_owner(_OPERATION_OWNER)
        threading.Thread(target=_watch_owner, args=(stop,), daemon=True).start()
        from opensquilla.sandbox.setup_state import _ensure_windows_setup_sync

        options: dict[str, Any] = {
            "on_unavailable": lambda detail: _emit(
                {"event": "unavailable", "detail": detail[:4096]}
            )
        }
        if repair_identity:
            options["repair_identity"] = True
        result = _ensure_windows_setup_sync(None, **options)
        _emit({"event": "result", "result": result.to_payload()})
        return 0
    except Exception as exc:
        _emit({"event": "result", "result": _failed(str(exc)[:4096]).to_payload()})
        return 1
    finally:
        stop.set()


if __name__ == "__main__":
    # Keep the operation context in the canonical module used by the native
    # helper payload encoder, also when Python starts this module with -m.
    from opensquilla.sandbox.backend.windows_setup_process import setup_launcher_main as main

    raise SystemExit(main())
