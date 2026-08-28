"""Linux seccomp helpers for sandboxed child processes."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
from collections.abc import Callable

from opensquilla.sandbox.types import NetworkMode

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

_COMMON_DENY_SYSCALLS_X86_64 = (
    101,  # ptrace
    310,  # process_vm_readv
    311,  # process_vm_writev
    425,  # io_uring_setup
    426,  # io_uring_enter
    427,  # io_uring_register
)
_RESTRICTED_DENY_SYSCALLS_X86_64 = (
    42,  # connect
    43,  # accept
    288,  # accept4
    49,  # bind
    50,  # listen
    52,  # getpeername
    51,  # getsockname
    48,  # shutdown
    44,  # sendto
    307,  # sendmmsg
    299,  # recvmmsg
    55,  # getsockopt
    54,  # setsockopt
)
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


def network_seccomp_preexec_from_policy(
    policy: dict[str, object],
) -> Callable[[], None] | None:
    network = str(policy.get("network", "none"))
    if network == NetworkMode.HOST.value:
        return None
    mode = "proxy" if network == NetworkMode.PROXY_ALLOWLIST.value else "restricted"
    if platform.machine() not in {"x86_64", "amd64"}:
        return None

    def apply_seccomp() -> None:
        install_network_seccomp(mode)

    return apply_seccomp


def install_network_seccomp(mode: str) -> None:
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
        _append_deny_socketpair_unless(instructions, allowed_domains=(_AF_UNIX,))
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
    if not allowed_domains:
        _append_deny_syscall(instructions, _SYS_SOCKET_X86_64)
        return
    instructions.append(_stmt(_BPF_LD_W_ABS, _SECCOMP_NR_OFFSET))
    instructions.append(
        _jump(_BPF_JMP_JEQ_K, _SYS_SOCKET_X86_64, 0, len(allowed_domains) + 2)
    )
    instructions.append(_stmt(_BPF_LD_W_ABS, _SECCOMP_ARG0_OFFSET))
    for index, domain in enumerate(allowed_domains):
        remaining_allowed_checks = len(allowed_domains) - index - 1
        instructions.append(
            _jump(
                _BPF_JMP_JEQ_K,
                domain,
                remaining_allowed_checks + 1,
                0,
            )
        )
    instructions.append(_stmt(_BPF_RET_K, _DENY))


def _append_deny_socketpair_unless(
    instructions: list[_SockFilter],
    *,
    allowed_domains: tuple[int, ...],
) -> None:
    instructions.append(_stmt(_BPF_LD_W_ABS, _SECCOMP_NR_OFFSET))
    instructions.append(_jump(_BPF_JMP_JEQ_K, _SYS_SOCKETPAIR_X86_64, 0, 3))
    instructions.append(_stmt(_BPF_LD_W_ABS, _SECCOMP_ARG0_OFFSET))
    instructions.append(_jump(_BPF_JMP_JEQ_K, allowed_domains[0], 1, 0))
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


__all__ = ["install_network_seccomp", "network_seccomp_preexec_from_policy"]
