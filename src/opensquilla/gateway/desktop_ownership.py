"""Verifiable ownership records for gateways spawned by the Desktop app.

The ordinary ``gateway.pid`` contract remains intentionally small and backward
compatible.  Desktop recovery needs stronger evidence before it may stop a
listener left behind by a crashed Electron process, so Desktop-spawned gateways
add a separate, profile-scoped record and a challenge/response proof.

The record contains a same-user secret and is therefore written with user-only
permissions.  HTTP responses never expose the secret or a filesystem path.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from opensquilla import __version__
from opensquilla.recovery.locking import profile_lock_key

DESKTOP_GATEWAY_OWNERSHIP_FILENAME: Final = "desktop-gateway.json"
DESKTOP_GATEWAY_OWNERSHIP_LOCK_FILENAME: Final = "desktop-gateway.lock"
DESKTOP_GATEWAY_OWNERSHIP_DIR_ENV: Final = (
    "OPENSQUILLA_DESKTOP_GATEWAY_OWNERSHIP_DIR"
)
DESKTOP_GATEWAY_OWNERSHIP_PROTOCOL: Final = (
    "opensquilla-desktop-gateway-ownership-v1"
)
DESKTOP_GATEWAY_OWNERSHIP_SCHEMA_VERSION: Final = 1
DESKTOP_GATEWAY_INSTANCE_NONCE_ENV: Final = (
    "OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_NONCE"
)

_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})
_PROOF_VALUE_RE = re.compile(r"^[0-9a-f]{64}$")
_CHALLENGE_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_INSTANCE_NONCE_RE = _CHALLENGE_VALUE_RE
_MAX_RECORD_BYTES = 16 * 1024
_RECORD_LOCK_TIMEOUT_SECONDS = 30.0


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def canonical_identity_payload(
    public_record: dict[str, Any], challenge: str
) -> bytes:
    """Return the exact cross-language byte payload signed by identity proofs."""

    return _canonical_payload({**public_record, "challenge": challenge})


def canonical_shutdown_payload(
    public_record: dict[str, Any], challenge: str
) -> bytes:
    """Return the domain-separated byte payload signed by shutdown requests."""

    return _canonical_payload(
        {**public_record, "action": "shutdown", "challenge": challenge}
    )


def valid_desktop_challenge(value: object) -> bool:
    return isinstance(value, str) and _CHALLENGE_VALUE_RE.fullmatch(value) is not None


def valid_desktop_proof(value: object) -> bool:
    return isinstance(value, str) and _PROOF_VALUE_RE.fullmatch(value) is not None


def _linux_process_start_identity(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # ``comm`` is parenthesized and may contain spaces or ``)``.  Fields
        # after its final close-paren begin at field 3; starttime is field 22.
        suffix = raw[raw.rfind(")") + 1 :].split()
        start_ticks = suffix[19]
    except (OSError, IndexError, ValueError):
        return None
    return f"linux-proc-start-ticks:{start_ticks}"


def _windows_process_start_identity(pid: int) -> str | None:
    try:
        import ctypes
        from ctypes import wintypes

        class FileTime(ctypes.Structure):
            _fields_ = [
                ("low", wintypes.DWORD),
                ("high", wintypes.DWORD),
            ]

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            int(pid),
        )
        if not handle:
            return None
        try:
            created = FileTime()
            exited = FileTime()
            kernel = FileTime()
            user = FileTime()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            filetime = (int(created.high) << 32) | int(created.low)
            return f"windows-creation-filetime:{filetime}"
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - this is a best-effort platform probe
        return None


def _posix_process_start_identity(pid: int) -> str | None:
    try:
        command = "/bin/ps" if Path("/bin/ps").is_file() else "ps"
        result = subprocess.run(
            [command, "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = " ".join(result.stdout.split())
    if result.returncode != 0 or not value:
        return None
    return f"posix-ps-lstart:{value}"


def process_start_identity(pid: int | None = None) -> str:
    """Return an opaque process-start marker suitable for strict comparison.

    Native OS creation identity is preferred.  The fallback is still unique
    to this runtime and is covered by the nonce proof; callers must treat the
    value as opaque rather than attempting to parse a platform-specific form.
    """

    process_id = os.getpid() if pid is None else int(pid)
    identity: str | None
    if sys.platform.startswith("linux"):
        identity = _linux_process_start_identity(process_id)
    elif os.name == "nt" or sys.platform == "win32":
        identity = _windows_process_start_identity(process_id)
    else:
        identity = _posix_process_start_identity(process_id)
    if identity is not None:
        return identity
    return (
        f"runtime-start:{process_id}:{time.time_ns()}:"
        f"{secrets.token_hex(16)}"
    )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.parent / (
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            try:
                fchmod(descriptor, 0o600)
            except OSError:
                pass
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            descriptor = None
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_RECORD_BYTES + 1)
        if len(raw) > _MAX_RECORD_BYTES:
            return None
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _try_lock_record(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt

        msvcrt_mod = cast(Any, msvcrt)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt_mod.locking(fd, msvcrt_mod.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_record(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt_mod = cast(Any, msvcrt)
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt_mod.locking(fd, msvcrt_mod.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def _ownership_record_lock(state_dir: Path) -> Iterator[None]:
    """Serialize record replacement/removal on one permanent lock inode."""

    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = state_dir / DESKTOP_GATEWAY_OWNERSHIP_LOCK_FILENAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    acquired = False
    try:
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode):
            raise OSError("Desktop gateway ownership lock must be a regular file")
        if value.st_size < 1:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b"\0")
            os.fsync(fd)
        with contextlib.suppress(OSError):
            os.chmod(lock_path, 0o600)

        deadline = time.monotonic() + _RECORD_LOCK_TIMEOUT_SECONDS
        while not _try_lock_record(fd):
            if time.monotonic() >= deadline:
                raise TimeoutError("Desktop gateway ownership record lock timed out")
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        acquired = True
        yield
    finally:
        try:
            if acquired:
                _unlock_record(fd)
        finally:
            os.close(fd)


@dataclass
class DesktopGatewayOwnership:
    """One Desktop-spawned gateway instance and its proof secret."""

    state_dir: Path
    profile_fingerprint: str
    port: int
    instance_nonce: str = field(repr=False)
    pid: int = field(default_factory=os.getpid)
    start_identity: str = field(default_factory=process_start_identity)
    version: str = __version__
    _active: bool = field(default=False, init=False, repr=False)
    _written_record: dict[str, Any] | None = field(
        default=None, init=False, repr=False
    )

    @classmethod
    def from_environment(
        cls,
        *,
        profile_home: str | Path,
        port: int,
    ) -> DesktopGatewayOwnership | None:
        desktop_enabled = (
            os.environ.get("OPENSQUILLA_DESKTOP", "").strip().lower()
            in _ENABLED_VALUES
        )
        nonce = os.environ.get(DESKTOP_GATEWAY_INSTANCE_NONCE_ENV, "").strip()
        if not desktop_enabled or not nonce:
            return None
        if _INSTANCE_NONCE_RE.fullmatch(nonce) is None:
            raise ValueError(
                f"{DESKTOP_GATEWAY_INSTANCE_NONCE_ENV} must be 32-128 "
                "base64url characters"
            )
        if not 1 <= int(port) <= 65535:
            raise ValueError("Desktop gateway ownership requires a concrete TCP port")
        raw_state_dir = os.environ.get(DESKTOP_GATEWAY_OWNERSHIP_DIR_ENV, "").strip()
        if not raw_state_dir:
            raise ValueError(
                f"{DESKTOP_GATEWAY_OWNERSHIP_DIR_ENV} is required for a Desktop gateway"
            )
        state_dir = Path(raw_state_dir).expanduser()
        if not state_dir.is_absolute():
            raise ValueError(
                f"{DESKTOP_GATEWAY_OWNERSHIP_DIR_ENV} must be an absolute path"
            )
        state_dir = state_dir.resolve(strict=False)
        profile_fingerprint = profile_lock_key(profile_home)
        if state_dir.name != profile_fingerprint:
            raise ValueError(
                f"{DESKTOP_GATEWAY_OWNERSHIP_DIR_ENV} must end with the profile fingerprint"
            )
        owner = cls(
            state_dir=state_dir,
            profile_fingerprint=profile_fingerprint,
            port=int(port),
            instance_nonce=nonce,
        )
        # The nonce is process-control authority, not provider/runtime config.
        # Remove both handoff values before service/channel subprocesses inherit
        # the Gateway environment; the active owner object retains what it needs.
        os.environ.pop(DESKTOP_GATEWAY_INSTANCE_NONCE_ENV, None)
        os.environ.pop(DESKTOP_GATEWAY_OWNERSHIP_DIR_ENV, None)
        return owner

    @property
    def path(self) -> Path:
        return self.state_dir / DESKTOP_GATEWAY_OWNERSHIP_FILENAME

    @property
    def public_record(self) -> dict[str, Any]:
        return {
            "schema_version": DESKTOP_GATEWAY_OWNERSHIP_SCHEMA_VERSION,
            "protocol": DESKTOP_GATEWAY_OWNERSHIP_PROTOCOL,
            "profile_fingerprint": self.profile_fingerprint,
            "pid": self.pid,
            "start_identity": self.start_identity,
            "port": self.port,
            "version": self.version,
        }

    @property
    def record(self) -> dict[str, Any]:
        return {**self.public_record, "instance_nonce": self.instance_nonce}

    def acquire(self) -> None:
        if self._active:
            return
        record = self.record
        with _ownership_record_lock(self.state_dir):
            _atomic_write_private_json(self.path, record)
        self._written_record = record
        self._active = True
        atexit.register(self.release)

    def release(self) -> None:
        """Remove only the exact record written by this gateway instance."""

        if not self._active:
            return
        self._active = False
        expected = self._written_record
        self._written_record = None
        if expected is None:
            return
        with _ownership_record_lock(self.state_dir):
            if _read_record(self.path) != expected:
                return
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                return
            _fsync_directory(self.path.parent)

    def identity_response(self, challenge: str) -> dict[str, Any]:
        payload = {**self.public_record, "challenge": challenge}
        payload["proof"] = hmac.new(
            self.instance_nonce.encode("ascii"),
            canonical_identity_payload(self.public_record, challenge),
            hashlib.sha256,
        ).hexdigest()
        return payload

    def verify_shutdown_proof(self, challenge: str, proof: str) -> bool:
        if not valid_desktop_challenge(challenge) or not valid_desktop_proof(proof):
            return False
        expected = hmac.new(
            self.instance_nonce.encode("ascii"),
            canonical_shutdown_payload(self.public_record, challenge),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(proof, expected)


_ACTIVE_DESKTOP_GATEWAY_OWNERSHIP: DesktopGatewayOwnership | None = None


def activate_desktop_gateway_ownership(
    *,
    profile_home: str | Path,
    port: int,
) -> DesktopGatewayOwnership | None:
    """Create the process-owned record when the Desktop handshake is enabled."""

    global _ACTIVE_DESKTOP_GATEWAY_OWNERSHIP
    owner = DesktopGatewayOwnership.from_environment(
        profile_home=profile_home,
        port=port,
    )
    if owner is None:
        return None
    active = _ACTIVE_DESKTOP_GATEWAY_OWNERSHIP
    if active is not None and active is not owner:
        raise RuntimeError("Desktop gateway ownership is already active")
    owner.acquire()
    _ACTIVE_DESKTOP_GATEWAY_OWNERSHIP = owner
    return owner


def release_active_desktop_gateway_ownership() -> None:
    """Release the active record after the outer profile writer lock exits."""

    global _ACTIVE_DESKTOP_GATEWAY_OWNERSHIP
    owner = _ACTIVE_DESKTOP_GATEWAY_OWNERSHIP
    _ACTIVE_DESKTOP_GATEWAY_OWNERSHIP = None
    if owner is not None:
        owner.release()


__all__ = [
    "DESKTOP_GATEWAY_INSTANCE_NONCE_ENV",
    "DESKTOP_GATEWAY_OWNERSHIP_DIR_ENV",
    "DESKTOP_GATEWAY_OWNERSHIP_FILENAME",
    "DESKTOP_GATEWAY_OWNERSHIP_LOCK_FILENAME",
    "DESKTOP_GATEWAY_OWNERSHIP_PROTOCOL",
    "DESKTOP_GATEWAY_OWNERSHIP_SCHEMA_VERSION",
    "DesktopGatewayOwnership",
    "activate_desktop_gateway_ownership",
    "canonical_identity_payload",
    "canonical_shutdown_payload",
    "process_start_identity",
    "release_active_desktop_gateway_ownership",
    "valid_desktop_challenge",
    "valid_desktop_proof",
]
