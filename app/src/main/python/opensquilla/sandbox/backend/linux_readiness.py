"""Readiness checks for the Linux sandbox backend."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

USER_NAMESPACE_FAILURES = (
    "loopback: Failed RTM_NEWADDR",
    "loopback: Failed RTM_NEWLINK",
    "setting up uid map: Permission denied",
    "No permissions to create a new namespace",
)


@dataclass(frozen=True)
class BwrapProbe:
    available: bool
    reason: str
    message: str
    path: str | None = None
    supports_argv0: bool = False
    supports_perms: bool = False
    supports_proc: bool = True


def proc_version_indicates_wsl1(proc_version: str) -> bool:
    text = proc_version.lower()
    remaining = text
    while "wsl" in remaining:
        marker = remaining.index("wsl")
        suffix = remaining[marker + len("wsl") :]
        digits = ""
        for char in suffix:
            if not char.isdigit():
                break
            digits += char
        if digits and int(digits) == 1:
            return True
        remaining = suffix
    return "microsoft" in text and "microsoft-standard" not in text


def is_wsl1() -> bool:
    try:
        return proc_version_indicates_wsl1(Path("/proc/version").read_text(encoding="utf-8"))
    except OSError:
        return False


def is_user_namespace_failure(output: subprocess.CompletedProcess[bytes]) -> bool:
    stderr = output.stderr.decode("utf-8", errors="replace")
    return any(pattern in stderr for pattern in USER_NAMESPACE_FAILURES)


def is_proc_mount_failure(output: subprocess.CompletedProcess[bytes] | str) -> bool:
    if isinstance(output, str):
        stderr = output
    else:
        stderr = output.stderr.decode("utf-8", errors="replace")
    lowered = stderr.lower()
    has_proc = "/proc" in lowered or "/newroot/proc" in lowered
    has_mount = (
        "mount proc" in lowered
        or "mounting proc" in lowered
        or "setting up /proc" in lowered
    )
    has_denial = (
        "invalid argument" in lowered
        or "operation not permitted" in lowered
        or "permission denied" in lowered
    )
    return has_proc and has_mount and has_denial


def probe_bwrap() -> BwrapProbe:
    if is_wsl1():
        return BwrapProbe(
            available=False,
            reason="wsl1_unsupported",
            message="Linux sandboxing with bubblewrap is not supported on WSL1.",
        )

    path = shutil.which("bwrap")
    if path is None:
        return BwrapProbe(
            available=False,
            reason="missing_bwrap",
            message="Bubblewrap is not installed or not present on PATH.",
        )

    help_output = subprocess.run(
        [path, "--help"],
        capture_output=True,
        timeout=2,
        check=False,
    )
    help_text = (
        help_output.stdout.decode("utf-8", errors="replace")
        + help_output.stderr.decode("utf-8", errors="replace")
    )
    supports_argv0 = "--argv0" in help_text
    supports_perms = "--perms" in help_text
    if not supports_perms:
        return BwrapProbe(
            available=False,
            reason="missing_bwrap_perms",
            message=(
                "Bubblewrap does not support the required --perms option needed "
                "for Linux sandbox metadata masks."
            ),
            path=path,
            supports_argv0=supports_argv0,
            supports_perms=False,
        )

    namespace_output = subprocess.run(
        [
            path,
            "--unshare-user",
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "/bin/true",
        ],
        capture_output=True,
        timeout=2,
        check=False,
    )
    if namespace_output.returncode != 0 and is_user_namespace_failure(namespace_output):
        return BwrapProbe(
            available=False,
            reason="user_namespace_unavailable",
            message="Bubblewrap cannot create the required Linux user namespace.",
            path=path,
            supports_argv0=supports_argv0,
            supports_perms=supports_perms,
        )

    proc_output = subprocess.run(
        [
            path,
            "--unshare-user",
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "/bin/true",
        ],
        capture_output=True,
        timeout=2,
        check=False,
    )
    supports_proc = proc_output.returncode == 0
    if proc_output.returncode != 0 and not is_proc_mount_failure(proc_output):
        if is_user_namespace_failure(proc_output):
            return BwrapProbe(
                available=False,
                reason="user_namespace_unavailable",
                message="Bubblewrap cannot create the required Linux user namespace.",
                path=path,
                supports_argv0=supports_argv0,
                supports_perms=supports_perms,
                supports_proc=False,
            )
        supports_proc = True

    return BwrapProbe(
        available=True,
        reason="ready",
        message="Bubblewrap is available for Linux sandboxing.",
        path=path,
        supports_argv0=supports_argv0,
        supports_perms=supports_perms,
        supports_proc=supports_proc,
    )
