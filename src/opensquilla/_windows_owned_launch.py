"""Minimal Windows child handshake, independent of parent ownership machinery.

The parent assigns the helper to its Job, waits for ready, records ownership,
then releases the gate. Keep target-only imports after that gate: importing
asyncio and the ownership registry here spends the bounded startup window on
code that runs exclusively in the parent.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import sys

_WINDOWS_HELPER_STRIP_ENV = "OPENSQUILLA_INTERNAL_PROCESS_TREE_STRIP_ENV"


def _windows_error(code: int | None = None) -> OSError:
    if code is None:
        code = int(getattr(ctypes, "get_last_error")())
    message = str(getattr(ctypes, "FormatError")(code)).strip()
    return OSError(code, message)


def _pop_windows_env(env: dict[str, str], key: str) -> str | None:
    folded = key.casefold()
    for candidate in tuple(env):
        if candidate.casefold() == folded:
            return env.pop(candidate)
    return None


def _windows_target_env_from_helper(helper_env) -> dict[str, str]:
    """Recover the caller-requested env after the helper has booted."""

    target_env = {str(key): str(value) for key, value in dict(helper_env).items()}
    injected = _pop_windows_env(target_env, _WINDOWS_HELPER_STRIP_ENV) or ""
    for key in injected.split(";"):
        if key:
            _pop_windows_env(target_env, key)
    return target_env


def _run_windows_owned_launch(
    gate_name: str,
    ready_name: str,
    argv: list[str],
) -> int:
    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    synchronize = 0x00100000
    event_modify_state = 0x0002
    infinite = 0xFFFFFFFF
    wait_failed = 0xFFFFFFFF
    gate = kernel32.OpenEventW(synchronize, False, gate_name)
    if not gate:
        raise _windows_error()
    ready = kernel32.OpenEventW(event_modify_state, False, ready_name)
    if not ready:
        kernel32.CloseHandle(gate)
        raise _windows_error()
    try:
        if not kernel32.SetEvent(ready):
            raise _windows_error()
        if kernel32.WaitForSingleObject(gate, infinite) == wait_failed:
            raise _windows_error()
    finally:
        kernel32.CloseHandle(ready)
        kernel32.CloseHandle(gate)
    if not argv:
        return 127

    # No target work, including its subprocess machinery, precedes gate release.
    import subprocess

    try:
        process = subprocess.Popen(
            argv,
            env=_windows_target_env_from_helper(os.environ),
        )
    except OSError:
        print("OpenSquilla controlled launch failed", file=sys.stderr)
        return 127
    return int(process.wait())


def main(argv: list[str] | None = None) -> int:
    """Run the fixed Windows helper mode without importing the parent module."""

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 4 and args[0] == "--windows-owned-launch" and args[3] == "--":
        return _run_windows_owned_launch(args[1], args[2], args[4:])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
