"""Standalone Linux exec wrapper used inside bubblewrap."""

from __future__ import annotations

import base64
import ctypes
import errno
import json
import os
import platform
import sys
from collections.abc import Sequence

try:
    import resource
except ImportError:
    resource = None  # type: ignore[assignment]

_PR_SET_NO_NEW_PRIVS = 38
_SECCOMP_SET_MODE_FILTER = 1
_SECCOMP_FILTER_FLAG_TSYNC = 1
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000
_DENY = _SECCOMP_RET_ERRNO | errno.EPERM

_BPF_LD_W_ABS = 0x20
_BPF_JMP_JEQ_K = 0x15
_BPF_RET_K = 0x06

_SECCOMP_NR_OFFSET = 0
_SECCOMP_ARG0_OFFSET = 16

_AF_UNIX = 1
_AF_INET = 2
_AF_INET6 = 10

_COMMON_DENY_SYSCALLS_X86_64 = (101, 310, 311, 425, 426, 427)
_RESTRICTED_DENY_SYSCALLS_X86_64 = (42, 43, 288, 49, 50, 52, 51, 48, 44, 307, 299, 55, 54)
_SYS_SOCKET_X86_64 = 41
_SYS_SOCKETPAIR_X86_64 = 53


class _SockFilter(ctypes.Structure):
    _fields_ = (
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint),
    )


class _SockFprog(ctypes.Structure):
    _fields_ = (
        ("len", ctypes.c_ushort),
        ("filter", ctypes.POINTER(_SockFilter)),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        separator = args.index("--")
    except ValueError:
        print("linux exec wrapper requires -- before command", file=sys.stderr)
        return 2
    if separator < 2 or args[0] != "--policy-b64":
        print("linux exec wrapper requires --policy-b64", file=sys.stderr)
        return 2
    command = args[separator + 1 :]
    if not command:
        print("linux exec wrapper requires a command", file=sys.stderr)
        return 2
    policy = _decode_policy(args[1])
    _apply_limits(policy)
    _apply_network_seccomp(policy)
    os.execvpe(command[0], command, os.environ)
    raise AssertionError("unreachable")


def _decode_policy(raw: str) -> dict[str, object]:
    decoded = base64.b64decode(raw.encode("ascii")).decode("utf-8")
    value = json.loads(decoded)
    if not isinstance(value, dict):
        raise ValueError("policy payload must decode to an object")
    return dict(value)


def _apply_limits(policy: dict[str, object]) -> None:
    if resource is None:
        return

    for resource_id, value in (
        (resource.RLIMIT_CPU, _positive_int(policy.get("cpuSeconds"))),
        (
            getattr(resource, "RLIMIT_NPROC", None),
            _positive_int(policy.get("pids")),
        ),
    ):
        if resource_id is None or value is None:
            continue
        try:
            _, hard = resource.getrlimit(resource_id)
            soft = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(resource_id, (soft, hard))
        except (OSError, ValueError):
            continue


def _positive_int(value: object) -> int | None:
    if not isinstance(value, (str, bytes, int, float)):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _apply_network_seccomp(policy: dict[str, object]) -> None:
    network = str(policy.get("network", "none"))
    if network == "host" or platform.machine() not in {"x86_64", "amd64"}:
        return
    mode = "proxy" if network == "proxy_allowlist" else "restricted"
    program = _build_filter(mode)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        _raise_errno("prctl(PR_SET_NO_NEW_PRIVS)")
    if libc.syscall(
        317,
        _SECCOMP_SET_MODE_FILTER,
        _SECCOMP_FILTER_FLAG_TSYNC,
        ctypes.byref(program),
    ) != 0:
        _raise_errno("seccomp(SECCOMP_SET_MODE_FILTER)")


def _build_filter(mode: str) -> _SockFprog:
    instructions: list[_SockFilter] = []
    for syscall_nr in _COMMON_DENY_SYSCALLS_X86_64:
        _append_deny_syscall(instructions, syscall_nr)
    if mode == "restricted":
        for syscall_nr in _RESTRICTED_DENY_SYSCALLS_X86_64:
            _append_deny_syscall(instructions, syscall_nr)
        _append_deny_socket_unless(instructions, allowed_domains=(_AF_UNIX,))
        _append_deny_socketpair_unless(instructions, allowed_domain=_AF_UNIX)
    elif mode == "proxy":
        _append_deny_socket_unless(instructions, allowed_domains=(_AF_INET, _AF_INET6))
        _append_deny_socketpair_if(instructions, denied_domain=_AF_UNIX)
    else:
        raise ValueError(f"unknown seccomp mode: {mode!r}")
    instructions.append(_stmt(_BPF_RET_K, _SECCOMP_RET_ALLOW))
    array_type = _SockFilter * len(instructions)
    array = array_type(*instructions)
    program = _SockFprog(len=len(instructions), filter=array)
    program._filter_array = array  # type: ignore[attr-defined]
    return program


def _append_deny_syscall(instructions: list[_SockFilter], syscall_nr: int) -> None:
    instructions.extend(
        (
            _stmt(_BPF_LD_W_ABS, _SECCOMP_NR_OFFSET),
            _jump(_BPF_JMP_JEQ_K, syscall_nr, 0, 1),
            _stmt(_BPF_RET_K, _DENY),
        )
    )


def _append_deny_socket_unless(
    instructions: list[_SockFilter],
    *,
    allowed_domains: tuple[int, ...],
) -> None:
    instructions.append(_stmt(_BPF_LD_W_ABS, _SECCOMP_NR_OFFSET))
    instructions.append(
        _jump(_BPF_JMP_JEQ_K, _SYS_SOCKET_X86_64, 0, len(allowed_domains) + 2)
    )
    instructions.append(_stmt(_BPF_LD_W_ABS, _SECCOMP_ARG0_OFFSET))
    for index, domain in enumerate(allowed_domains):
        remaining = len(allowed_domains) - index - 1
        instructions.append(_jump(_BPF_JMP_JEQ_K, domain, remaining + 1, 0))
    instructions.append(_stmt(_BPF_RET_K, _DENY))


def _append_deny_socketpair_unless(
    instructions: list[_SockFilter],
    *,
    allowed_domain: int,
) -> None:
    instructions.append(_stmt(_BPF_LD_W_ABS, _SECCOMP_NR_OFFSET))
    instructions.append(_jump(_BPF_JMP_JEQ_K, _SYS_SOCKETPAIR_X86_64, 0, 3))
    instructions.append(_stmt(_BPF_LD_W_ABS, _SECCOMP_ARG0_OFFSET))
    instructions.append(_jump(_BPF_JMP_JEQ_K, allowed_domain, 1, 0))
    instructions.append(_stmt(_BPF_RET_K, _DENY))


def _append_deny_socketpair_if(
    instructions: list[_SockFilter],
    *,
    denied_domain: int,
) -> None:
    instructions.append(_stmt(_BPF_LD_W_ABS, _SECCOMP_NR_OFFSET))
    instructions.append(_jump(_BPF_JMP_JEQ_K, _SYS_SOCKETPAIR_X86_64, 0, 3))
    instructions.append(_stmt(_BPF_LD_W_ABS, _SECCOMP_ARG0_OFFSET))
    instructions.append(_jump(_BPF_JMP_JEQ_K, denied_domain, 0, 1))
    instructions.append(_stmt(_BPF_RET_K, _DENY))


def _stmt(code: int, k: int) -> _SockFilter:
    return _SockFilter(code=code, jt=0, jf=0, k=k)


def _jump(code: int, k: int, jt: int, jf: int) -> _SockFilter:
    return _SockFilter(code=code, jt=jt, jf=jf, k=k)


def _raise_errno(operation: str) -> None:
    err = ctypes.get_errno()
    raise OSError(err, f"{operation} failed: {os.strerror(err)}")


if __name__ == "__main__":
    raise SystemExit(main())
