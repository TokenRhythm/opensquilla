"""Secure downloader, extractor, and activation manager for toolchains."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from opensquilla.paths import state_dir
from opensquilla.skills.toolchains import registry
from opensquilla.skills.toolchains.registry import ToolchainDescriptor

_LAYOUT_VERSION = "v1"
_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
_MAX_EXPANSION_RATIO = 100
_COPY_CHUNK_SIZE = 1024 * 1024
_PROBE_TIMEOUT_SECONDS = 30.0
_CODESIGN_PATH = Path("/usr/bin/codesign")
_POST_INSTALL_TIMEOUT_SECONDS = 600.0
_INSTALL_LOCK_TIMEOUT_SECONDS = 900.0
_SAFE_COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SAFE_RECEIPT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PAYLOAD_MANIFEST_VERSION = 1
_PAPER_CJK_LINEBREAK_LINES = (
    r'\XeTeXlinebreaklocale "zh"',
    r"\XeTeXlinebreakskip = 0pt plus 1pt",
)
_PAPER_CJK_WRAP_PROBE = (
    "受管工具链必须为连续中文正文提供自然断点并保持页面边界。"
    "这段文字用于覆盖真实论文中的中文短语、常用标点、引用说明与排版检查，"
    "任何缺字、严重越界或未解析引用都必须阻止安装被标记为可用。"
)

ProgressCallback = Callable[[int, int], None]
ProbeCallback = Callable[[ToolchainDescriptor, Path, tuple[Path, ...]], bool | None]


class ToolchainError(RuntimeError):
    """Base error for managed toolchain operations."""


class UnsupportedToolchainError(ToolchainError):
    """Raised when the current platform has no safely installable artifact."""


class DownloadVerificationError(ToolchainError):
    """Raised when downloaded bytes do not match the pinned catalog entry."""


class UnsafeArchiveError(ToolchainError):
    """Raised when an archive violates extraction safety limits."""


class ToolchainProbeError(ToolchainError):
    """Raised when an extracted toolchain fails its catalog probes."""


@dataclass(frozen=True)
class ActivationReceipt:
    """Durable description of the currently activated component package."""

    component_id: str
    version: str
    platform_key: str
    sha256: str
    install_backend: str
    package_relpath: str | None
    external_root: str | None
    bin_relpaths: tuple[str, ...]
    resources: dict[str, str]
    activated_at_ms: int
    receipt_id: str


@dataclass(frozen=True)
class CapabilityReport:
    """Effective runtime capability for one component on the current host."""

    component_id: str
    version: str
    platform_key: str
    supported: bool
    ready: bool
    reason: str
    binaries: dict[str, str]
    resources: dict[str, str]
    checked_at_ms: int


_PROBE_CACHE_TTL_SECONDS = 30.0
_probe_cache: dict[str, tuple[float, CapabilityReport]] = {}
_probe_cache_lock = threading.Lock()
_component_thread_locks: dict[str, threading.Lock] = {}
_component_thread_locks_guard = threading.Lock()
_configured_state_dir: ContextVar[Path | None] = ContextVar(
    "opensquilla_managed_toolchain_state_dir",
    default=None,
)


@contextmanager
def managed_toolchain_state_scope(configured_state_dir: str | Path | None):
    """Bind managed toolchain storage to one task's configured state directory."""

    if configured_state_dir is None or (
        isinstance(configured_state_dir, str) and not configured_state_dir.strip()
    ):
        yield
        return
    token = _configured_state_dir.set(Path(configured_state_dir).expanduser())
    try:
        yield
    finally:
        _configured_state_dir.reset(token)


def toolchains_root(root: Path | None = None) -> Path:
    """Return the schema-versioned managed toolchain state root."""
    if root is not None:
        return Path(root)
    configured = _configured_state_dir.get()
    if configured is not None:
        return configured / "toolchains" / _LAYOUT_VERSION
    gateway_state = os.environ.get("OPENSQUILLA_GATEWAY_STATE_DIR", "").strip()
    if gateway_state:
        return Path(gateway_state).expanduser() / "toolchains" / _LAYOUT_VERSION
    return state_dir("toolchains", _LAYOUT_VERSION)


def _ensure_root(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        # ACLs and Windows can reject POSIX mode changes; containment is still
        # enforced by path validation and fixed catalog identifiers.
        pass
    for name in ("packages", "active", "receipts", "staging", "locks"):
        (root / name).mkdir(mode=0o700, exist_ok=True)


def _try_file_lock(handle: Any) -> bool:
    if os.name == "nt":
        import msvcrt

        msvcrt_api: Any = msvcrt
        handle.seek(0)
        try:
            msvcrt_api.locking(handle.fileno(), msvcrt_api.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        return False
    return True


def _release_file_lock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt_api: Any = msvcrt
        handle.seek(0)
        msvcrt_api.locking(handle.fileno(), msvcrt_api.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _ComponentInstallLock:
    def __init__(self, root: Path, component_id: str, timeout: float) -> None:
        self.root = root
        self.component_id = component_id
        self.timeout = max(0.0, timeout)
        self.handle: Any = None
        self.thread_lock: threading.Lock | None = None

    def __enter__(self) -> _ComponentInstallLock:
        key = f"{self.root.absolute()}::{self.component_id}"
        with _component_thread_locks_guard:
            self.thread_lock = _component_thread_locks.setdefault(key, threading.Lock())
        if not self.thread_lock.acquire(timeout=self.timeout):
            raise ToolchainError(
                f"Timed out waiting for another {self.component_id} setup to finish"
            )
        deadline = time.monotonic() + self.timeout
        path = self.root / "locks" / f"{self.component_id}.lock"
        try:
            self.handle = path.open("a+b")
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(b"\0")
                self.handle.flush()
            while not _try_file_lock(self.handle):
                if time.monotonic() >= deadline:
                    raise ToolchainError(
                        f"Timed out waiting for another {self.component_id} setup to finish"
                    )
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            return self
        except BaseException:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            self.thread_lock.release()
            self.thread_lock = None
            raise

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        try:
            if self.handle is not None:
                _release_file_lock(self.handle)
                self.handle.close()
                self.handle = None
        finally:
            if self.thread_lock is not None:
                self.thread_lock.release()
                self.thread_lock = None


def _notify(progress_cb: ProgressCallback | None, current: int, total: int) -> None:
    if progress_cb is not None:
        progress_cb(current, total)


def _download(
    descriptor: ToolchainDescriptor,
    destination: Path,
    progress_cb: ProgressCallback | None,
    *,
    progress_offset: int = 0,
    progress_total: int | None = None,
) -> None:
    if descriptor.url is None or descriptor.sha256 is None or descriptor.size is None:
        raise UnsupportedToolchainError(descriptor.unsupported_reason or "Artifact is unavailable")
    _download_pinned(
        descriptor.url,
        descriptor.sha256,
        descriptor.size,
        destination,
        progress_cb,
        progress_offset=progress_offset,
        progress_total=progress_total,
    )


def _download_pinned(
    url: str,
    sha256: str,
    size: int,
    destination: Path,
    progress_cb: ProgressCallback | None,
    *,
    progress_offset: int = 0,
    progress_total: int | None = None,
) -> None:
    if not url.startswith("https://"):
        raise DownloadVerificationError("Managed artifacts must use HTTPS")
    if size <= 0 or size > _MAX_DOWNLOAD_BYTES:
        raise DownloadVerificationError("Pinned artifact size exceeds the download safety limit")
    total = progress_total if progress_total is not None else size

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "OpenSquilla-managed-toolchain/1"},
    )
    digest = hashlib.sha256()
    downloaded = 0
    _notify(progress_cb, progress_offset, total)
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed catalog URL
        final_url = response.geturl()
        if not final_url.startswith("https://"):
            raise DownloadVerificationError("Artifact redirect left HTTPS")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                announced = int(content_length)
            except ValueError as exc:
                raise DownloadVerificationError(
                    "Artifact returned an invalid Content-Length"
                ) from exc
            if announced != size:
                raise DownloadVerificationError(
                    f"Artifact size mismatch: expected {size}, server announced {announced}"
                )

        with destination.open("xb") as output:
            while chunk := response.read(_COPY_CHUNK_SIZE):
                downloaded += len(chunk)
                if downloaded > size or downloaded > _MAX_DOWNLOAD_BYTES:
                    raise DownloadVerificationError("Artifact exceeded its pinned size")
                output.write(chunk)
                digest.update(chunk)
                _notify(progress_cb, progress_offset + downloaded, total)

    if downloaded != size:
        raise DownloadVerificationError(
            f"Artifact size mismatch: expected {size}, received {downloaded}"
        )
    actual_digest = digest.hexdigest()
    if not hmac.compare_digest(actual_digest, sha256):
        raise DownloadVerificationError("Artifact SHA-256 did not match the pinned catalog digest")


def _safe_archive_name(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise UnsafeArchiveError(f"Unsafe archive path: {name!r}")
    if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        raise UnsafeArchiveError(f"Absolute archive path is not allowed: {name!r}")
    path = PurePosixPath(name)
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if not parts or ".." in parts:
        raise UnsafeArchiveError(f"Archive path traversal is not allowed: {name!r}")
    return PurePosixPath(*parts)


def _extraction_limit(compressed_size: int) -> int:
    ratio_limit = max(compressed_size, compressed_size * _MAX_EXPANSION_RATIO)
    return min(_MAX_EXTRACTED_BYTES, ratio_limit)


def _claim_destination(
    destination_root: Path,
    relative: PurePosixPath,
    seen: set[str],
) -> Path:
    key = relative.as_posix().casefold()
    if key in seen:
        raise UnsafeArchiveError(f"Duplicate archive path: {relative.as_posix()}")
    seen.add(key)
    destination = destination_root.joinpath(*relative.parts)
    try:
        destination.relative_to(destination_root)
    except ValueError as exc:
        raise UnsafeArchiveError("Archive entry escaped the extraction root") from exc
    return destination


def _resolve_tar_link_target(
    member_path: PurePosixPath,
    linkname: str,
    *,
    hardlink: bool,
) -> PurePosixPath:
    if not linkname or "\x00" in linkname or "\\" in linkname:
        raise UnsafeArchiveError(f"Unsafe archive link target: {linkname!r}")
    if linkname.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", linkname):
        raise UnsafeArchiveError(f"Absolute archive link target is not allowed: {linkname!r}")
    resolved = [] if hardlink else list(member_path.parent.parts)
    for part in PurePosixPath(linkname).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise UnsafeArchiveError(f"Archive link target escapes its root: {linkname!r}")
            resolved.pop()
            continue
        resolved.append(part)
    if not resolved or resolved[0] != member_path.parts[0]:
        raise UnsafeArchiveError(f"Archive link target escapes its root: {linkname!r}")
    return PurePosixPath(*resolved)


def _validate_tar_members(
    archive: Path,
    destination: Path,
    compressed_size: int,
) -> tuple[
    list[tarfile.TarInfo],
    dict[PurePosixPath, PurePosixPath],
    dict[PurePosixPath, PurePosixPath],
]:
    total_size = 0
    seen: set[str] = set()
    limit = _extraction_limit(compressed_size)
    members: list[tarfile.TarInfo] = []
    paths: dict[PurePosixPath, tarfile.TarInfo] = {}
    with tarfile.open(archive, mode="r|xz") as source:
        for member in source:
            if len(members) >= _MAX_ARCHIVE_MEMBERS:
                raise UnsafeArchiveError("Archive contains too many entries")
            relative = _safe_archive_name(member.name)
            _claim_destination(destination, relative, seen)
            if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                raise UnsafeArchiveError(
                    f"Archive contains a device or special entry: {member.name!r}"
                )
            if member.isfile() and member.size < 0:
                raise UnsafeArchiveError("Archive contains a negative file size")
            if member.isfile():
                total_size += member.size
            if total_size > limit:
                raise UnsafeArchiveError("Archive exceeds the extracted-size safety limit")
            members.append(member)
            paths[relative] = member

    link_targets: dict[PurePosixPath, PurePosixPath] = {}
    for member in members:
        relative = _safe_archive_name(member.name)
        if member.issym() or member.islnk():
            target = _resolve_tar_link_target(
                relative,
                member.linkname,
                hardlink=member.islnk(),
            )
            if target not in paths:
                raise UnsafeArchiveError(
                    f"Archive link target is missing: {member.linkname!r}"
                )
            link_targets[relative] = target

    for relative in paths:
        for index in range(1, len(relative.parts)):
            ancestor = PurePosixPath(*relative.parts[:index])
            ancestor_member = paths.get(ancestor)
            if ancestor_member is not None and (
                ancestor_member.issym() or ancestor_member.islnk()
            ):
                raise UnsafeArchiveError(
                    f"Archive entry descends through a link: {relative.as_posix()!r}"
                )

    final_targets: dict[PurePosixPath, PurePosixPath] = {}

    def resolve_final(relative: PurePosixPath, stack: set[PurePosixPath]) -> PurePosixPath:
        if relative in stack:
            raise UnsafeArchiveError("Archive contains a cyclic link chain")
        member = paths[relative]
        if not (member.issym() or member.islnk()):
            return relative
        final = resolve_final(link_targets[relative], {*stack, relative})
        if member.islnk() and not paths[final].isfile():
            raise UnsafeArchiveError("Archive hardlink does not resolve to a regular file")
        return final

    for relative in link_targets:
        final_targets[relative] = resolve_final(relative, set())
    return members, link_targets, final_targets


def _extract_tar_xz(archive: Path, destination: Path, compressed_size: int) -> None:
    members, _link_targets, final_targets = _validate_tar_members(
        archive, destination, compressed_size
    )
    expected = iter(members)
    with tarfile.open(archive, mode="r|xz") as source:
        for member in source:
            validated = next(expected, None)
            if validated is None or (
                member.name,
                member.type,
                member.size,
                member.linkname,
            ) != (
                validated.name,
                validated.type,
                validated.size,
                validated.linkname,
            ):
                raise UnsafeArchiveError("Archive changed during validated extraction")
            relative = _safe_archive_name(member.name)
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            stream = source.extractfile(member)
            if stream is None:
                raise UnsafeArchiveError(f"Archive member was not readable: {member.name!r}")
            copied = 0
            with stream, target.open("xb") as output:
                while chunk := stream.read(_COPY_CHUNK_SIZE):
                    copied += len(chunk)
                    if copied > member.size:
                        raise UnsafeArchiveError("Archive member exceeded its declared size")
                    output.write(chunk)
            if copied != member.size:
                raise UnsafeArchiveError("Archive member was shorter than its declared size")
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    if next(expected, None) is not None:
        raise UnsafeArchiveError("Archive changed during validated extraction")

    members_by_path = {_safe_archive_name(member.name): member for member in members}
    for member in members:
        if not member.islnk():
            continue
        relative = _safe_archive_name(member.name)
        target = destination.joinpath(*relative.parts)
        final = destination.joinpath(*final_targets[relative].parts)
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        os.link(final, target, follow_symlinks=False)
    for member in members:
        if not member.issym():
            continue
        relative = _safe_archive_name(member.name)
        target = destination.joinpath(*relative.parts)
        final_member = members_by_path[final_targets[relative]]
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        os.symlink(member.linkname, target, target_is_directory=final_member.isdir())


def _zip_entry_kind(info: zipfile.ZipInfo) -> str:
    mode = info.external_attr >> 16
    if info.is_dir():
        return "directory"
    file_type = stat.S_IFMT(mode)
    if file_type == stat.S_IFLNK:
        return "special"
    if file_type not in {0, stat.S_IFREG}:
        return "special"
    return "file"


def _extract_zip(archive: Path, destination: Path, compressed_size: int) -> None:
    total_size = 0
    seen: set[str] = set()
    limit = _extraction_limit(compressed_size)
    with zipfile.ZipFile(archive) as source:
        infos = source.infolist()
        if len(infos) > _MAX_ARCHIVE_MEMBERS:
            raise UnsafeArchiveError("Archive contains too many entries")
        for info in infos:
            if info.flag_bits & 0x1:
                raise UnsafeArchiveError("Encrypted archive members are not supported")
            relative = _safe_archive_name(info.filename)
            target = _claim_destination(destination, relative, seen)
            kind = _zip_entry_kind(info)
            if kind == "directory":
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            if kind != "file":
                raise UnsafeArchiveError(
                    f"Archive contains a link, device, or special entry: {info.filename!r}"
                )
            total_size += info.file_size
            if total_size > limit:
                raise UnsafeArchiveError("Archive exceeds the extracted-size safety limit")
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            copied = 0
            with source.open(info) as stream, target.open("xb") as output:
                while chunk := stream.read(_COPY_CHUNK_SIZE):
                    copied += len(chunk)
                    if copied > info.file_size:
                        raise UnsafeArchiveError("Archive member exceeded its declared size")
                    output.write(chunk)
            if copied != info.file_size:
                raise UnsafeArchiveError("Archive member was shorter than its declared size")
            unix_mode = info.external_attr >> 16
            target.chmod(0o755 if unix_mode & 0o111 else 0o644)


def _extract_archive(
    archive: Path,
    destination: Path,
    archive_type: str,
    compressed_size: int,
) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    if archive_type == "tar.xz":
        _extract_tar_xz(archive, destination, compressed_size)
        return
    if archive_type == "zip":
        _extract_zip(archive, destination, compressed_size)
        return
    raise UnsupportedToolchainError(f"Unsupported managed archive type: {archive_type}")


def _relocate_cataloged_archive_member(
    payload: Path,
    *,
    member_name: str | None,
    destination_name: str | None,
) -> None:
    """Move a code-owned single-file archive member to its runtime location."""

    if member_name is None and destination_name is None:
        return
    if not member_name or not destination_name:
        raise UnsupportedToolchainError("Managed archive relocation is incomplete")
    member = _safe_archive_name(member_name)
    destination_relative = _safe_archive_name(destination_name)
    source = payload.joinpath(*member.parts)
    destination = payload.joinpath(*destination_relative.parts)
    files = [
        candidate
        for candidate in payload.rglob("*")
        if candidate.is_file() and not candidate.is_symlink()
    ]
    if source.is_symlink() or not source.is_file() or files != [source]:
        raise UnsafeArchiveError(
            "Managed single-file archive did not contain exactly its cataloged member"
        )
    if destination == source or destination.exists():
        raise UnsafeArchiveError("Managed archive relocation destination is invalid")
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=False)
    os.replace(source, destination)
    destination.chmod(0o755)


def _install_auxiliary_assets(
    descriptor: ToolchainDescriptor,
    payload: Path,
    progress_cb: ProgressCallback | None,
    *,
    progress_offset: int,
    progress_total: int,
) -> dict[str, str]:
    resources: dict[str, str] = {}
    offset = progress_offset
    for asset in descriptor.auxiliary_assets:
        relative = _safe_archive_name(asset.destination)
        destination = payload.joinpath(*relative.parts)
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if asset.archive_type is None:
            _download_pinned(
                asset.url,
                asset.sha256,
                asset.size,
                destination,
                progress_cb,
                progress_offset=offset,
                progress_total=progress_total,
            )
        else:
            if asset.archive_type not in {"tar.xz", "zip"} or not asset.archive_member:
                raise UnsupportedToolchainError(
                    "Managed auxiliary archive has no safe cataloged member"
                )
            member = _safe_archive_name(asset.archive_member)
            with tempfile.TemporaryDirectory(
                prefix=f"{asset.asset_id}-", dir=payload.parent
            ) as temp_name:
                temp_root = Path(temp_name)
                archive = temp_root / "artifact"
                _download_pinned(
                    asset.url,
                    asset.sha256,
                    asset.size,
                    archive,
                    progress_cb,
                    progress_offset=offset,
                    progress_total=progress_total,
                )
                extracted = temp_root / "extracted"
                _extract_archive(archive, extracted, asset.archive_type, asset.size)
                source = extracted.joinpath(*member.parts)
                extracted_files = [
                    candidate
                    for candidate in extracted.rglob("*")
                    if candidate.is_file() and not candidate.is_symlink()
                ]
                if source.is_symlink() or not source.is_file() or extracted_files != [source]:
                    raise UnsafeArchiveError(
                        "Managed auxiliary archive did not contain exactly its cataloged file"
                    )
                if destination.exists():
                    raise UnsafeArchiveError("Managed auxiliary destination already exists")
                shutil.copyfile(source, destination)
        destination.chmod(0o755 if asset.executable else 0o644)
        resources[asset.asset_id] = relative.as_posix()
        offset += asset.size
    return resources


def _find_payload_bins(payload: Path, descriptor: ToolchainDescriptor) -> tuple[Path, ...]:
    bins: list[Path] = []
    for relpath in descriptor.bin_relpaths:
        relative = _safe_archive_name(relpath)
        candidate = payload.joinpath(*relative.parts)
        if not candidate.is_dir():
            raise ToolchainProbeError(f"Managed toolchain bin directory is missing: {relpath}")
        bins.append(candidate)
    if not bins:
        raise ToolchainProbeError("Managed toolchain has no cataloged bin directory")
    return tuple(bins)


def _find_in_bins(name: str, bin_dirs: tuple[Path, ...]) -> Path | None:
    candidates = [name]
    if os.name == "nt" and not Path(name).suffix:
        candidates.extend(f"{name}{suffix.lower()}" for suffix in (".EXE", ".BAT", ".CMD"))
    for directory in bin_dirs:
        for candidate_name in candidates:
            candidate = directory / candidate_name
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _run_probes(
    descriptor: ToolchainDescriptor,
    payload: Path,
    bin_dirs: tuple[Path, ...],
    probe_cb: ProbeCallback | None,
) -> None:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([*(str(path) for path in bin_dirs), env.get("PATH", "")])
    for command in descriptor.probe_commands:
        if not command:
            continue
        executable = _find_in_bins(command[0], bin_dirs)
        if executable is None:
            raise ToolchainProbeError(f"Managed toolchain probe is missing: {command[0]}")
        try:
            completed = subprocess.run(
                [str(executable), *command[1:]],
                check=False,
                capture_output=True,
                env=env,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToolchainProbeError(f"Managed toolchain probe failed: {command[0]}") from exc
        if completed.returncode != 0:
            raise ToolchainProbeError(
                f"Managed toolchain probe exited {completed.returncode}: {command[0]}"
            )
    if probe_cb is not None and probe_cb(descriptor, payload, bin_dirs) is False:
        raise ToolchainProbeError("Managed toolchain validation callback rejected the install")


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    label: str,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            cwd=cwd,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolchainProbeError(
            f"Managed toolchain {label} timed out after {timeout:g} seconds"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolchainProbeError(f"Managed toolchain {label} failed to run") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        detail = stderr[-500:] if stderr else f"exit status {completed.returncode}"
        raise ToolchainProbeError(f"Managed toolchain {label} failed: {detail}")
    return completed


def _validate_paper_probe_quality(
    probe_root: Path,
    final_xelatex: subprocess.CompletedProcess[bytes],
) -> None:
    """Reject a TeX closure that compiles but degrades real CJK output."""

    quality_log = (final_xelatex.stdout + final_xelatex.stderr).decode(
        "utf-8", errors="replace"
    )
    paper_log = probe_root / "paper.log"
    if paper_log.is_file():
        quality_log += "\n" + paper_log.read_text(encoding="utf-8", errors="replace")
    missing_glyphs = sorted(
        set(re.findall(r"^Missing character:.*$", quality_log, re.MULTILINE))
    )
    severe_overfull = [
        float(value)
        for value in re.findall(
            r"Overfull \\hbox \((\d+(?:\.\d+)?)pt too wide\)",
            quality_log,
        )
        if float(value) >= 20.0
    ]
    unresolved = sorted(
        {
            line.strip()
            for line in quality_log.splitlines()
            if re.search(
                r"LaTeX Warning: (?:Citation|Reference) .+ undefined|"
                r"There were undefined references",
                line,
            )
        }
    )
    if not (missing_glyphs or severe_overfull or unresolved):
        return
    details: list[str] = ["Paper toolchain output-quality smoke test failed"]
    if missing_glyphs:
        details.append(f"missing glyph warnings: {len(missing_glyphs)}")
    if severe_overfull:
        details.append(
            f"layout overflow: max={max(severe_overfull):.2f}pt threshold=20.00pt"
        )
    if unresolved:
        details.append(f"unresolved references: {len(unresolved)}")
    raise ToolchainProbeError("; ".join(details))


def _validate_paper_probe_source(source: str) -> None:
    if any(line not in source for line in _PAPER_CJK_LINEBREAK_LINES):
        raise ToolchainProbeError("Paper toolchain smoke test is missing CJK line breaking")
    probe_lines = [line for line in source.splitlines() if _PAPER_CJK_WRAP_PROBE in line]
    if probe_lines != [_PAPER_CJK_WRAP_PROBE]:
        raise ToolchainProbeError(
            "Paper toolchain smoke test must contain one uninterrupted CJK wrap probe"
        )


def _paper_probe_source() -> str:
    source = r"""\documentclass{article}
\usepackage{fontspec}
\usepackage{amsmath}
\usepackage{booktabs}
\usepackage{geometry}
\usepackage{hyperref}
\setmainfont[FontIndex=2]{NotoSansCJK-Regular.ttc}
\XeTeXlinebreaklocale "zh"
\XeTeXlinebreakskip = 0pt plus 1pt
\begin{document}
\section{Capability smoke test}
__OPENSQUILLA_CJK_WRAP_PROBE__
The identity $e^{i\pi}+1=0$ is cited here~\cite{smoke}.

第二个自然段再次验证中文换行。
下载校验、字体发现、参考文献解析、数学公式、表格环境和超链接必须在同一个离线能力探针中协同工作；
如果编译日志出现缺字、严重越界或未解析引用，安装就不能被标记为可用。
\begin{tabular}{lr}\toprule Item & Value\\\midrule Test & 1\\\bottomrule\end{tabular}
\bibliographystyle{plain}
\bibliography{refs}
\end{document}
""".replace("__OPENSQUILLA_CJK_WRAP_PROBE__", _PAPER_CJK_WRAP_PROBE)
    _validate_paper_probe_source(source)
    return source


def _capability_resource_path(
    descriptor: ToolchainDescriptor,
    payload: Path,
    asset_id: str,
    resource_paths: Mapping[str, Path] | None,
) -> Path:
    if resource_paths is not None:
        selected = resource_paths.get(asset_id)
        if selected is not None:
            return selected
    asset = next(
        (item for item in descriptor.auxiliary_assets if item.asset_id == asset_id),
        None,
    )
    if asset is None:
        # Keep the capability helper independently testable and compatible
        # with early descriptors that predated explicit resource metadata.
        if asset_id == "noto-cjk-font":
            return payload / "fonts/NotoSansCJK-Regular.ttc"
        return payload / "__missing_managed_resource__"
    relative = _safe_archive_name(asset.destination)
    return payload.joinpath(*relative.parts)


def _paper_capability(
    descriptor: ToolchainDescriptor,
    payload: Path,
    bin_dirs: tuple[Path, ...],
    *,
    env_override: Mapping[str, str] | None = None,
    resource_paths: Mapping[str, Path] | None = None,
) -> None:
    """Capability-test CJK, bibliography, math, table, and hyperlink output."""
    executables: dict[str, Path] = {}
    for name in ("kpsewhich", "xelatex", "bibtex"):
        executable = _find_in_bins(name, bin_dirs)
        if executable is None:
            raise ToolchainProbeError(f"Paper toolchain is missing: {name}")
        executables[name] = executable
    cjk_font = _capability_resource_path(
        descriptor,
        payload,
        "noto-cjk-font",
        resource_paths,
    )
    if not cjk_font.is_file():
        raise ToolchainProbeError("Paper toolchain is missing its pinned CJK font")
    if env_override is None:
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join(
            [*(str(path) for path in bin_dirs), env.get("PATH", "")]
        )
    else:
        env = dict(env_override)
    existing_font_dirs = env.get("OSFONTDIR", "")
    env["OSFONTDIR"] = os.pathsep.join(
        value for value in (str(cjk_font.parent), existing_font_dirs) if value
    )

    for filename in ("fontspec.sty", "NotoSansCJK-Regular.ttc"):
        _run_checked(
            [str(executables["kpsewhich"]), filename],
            cwd=payload,
            env=env,
            timeout=_PROBE_TIMEOUT_SECONDS,
            label=f"TeX capability probe ({filename})",
        )

    with tempfile.TemporaryDirectory(prefix="opensquilla-paper-probe-") as temp_name:
        probe_root = Path(temp_name)
        (probe_root / "paper.tex").write_text(_paper_probe_source(), encoding="utf-8")
        (probe_root / "refs.bib").write_text(
            "@book{smoke, title={OpenSquilla Smoke Test}, author={Example, Ada}, year={2026}}\n",
            encoding="utf-8",
        )
        xelatex = [
            str(executables["xelatex"]),
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-no-shell-escape",
            "paper.tex",
        ]
        _run_checked(
            xelatex,
            cwd=probe_root,
            env=env,
            timeout=120,
            label="XeLaTeX CJK smoke test",
        )
        _run_checked(
            [str(executables["bibtex"]), "paper"],
            cwd=probe_root,
            env=env,
            timeout=60,
            label="BibTeX smoke test",
        )
        _run_checked(
            xelatex,
            cwd=probe_root,
            env=env,
            timeout=120,
            label="XeLaTeX bibliography smoke test",
        )
        final_xelatex = _run_checked(
            xelatex,
            cwd=probe_root,
            env=env,
            timeout=120,
            label="XeLaTeX final smoke test",
        )
        _validate_paper_probe_quality(probe_root, final_xelatex)
        if not (probe_root / "paper.pdf").is_file():
            raise ToolchainProbeError("Paper toolchain smoke test did not produce a PDF")


def _prepare_macos_media_payload(
    descriptor: ToolchainDescriptor,
    payload: Path,
    bin_dirs: tuple[Path, ...],
) -> None:
    """Replace invalid upstream signatures with deterministic local ad-hoc ones.

    The remote ZIP hashes remain the trust anchor. This code-owned transform
    runs before the complete payload manifest is written. Ad-hoc signing gives
    Apple Silicon a valid CodeDirectory; it is not Apple notarization.
    """

    if not (
        descriptor.component_id == "media-ffmpeg"
        and descriptor.install_backend == "archive"
        and descriptor.platform_key.startswith("darwin-")
    ):
        return
    codesign = _CODESIGN_PATH
    if not codesign.is_file():
        raise UnsupportedToolchainError("macOS codesign is required for managed FFmpeg")
    payload_root = payload.resolve(strict=True)
    env = dict(os.environ)
    for name in ("ffmpeg", "ffprobe"):
        executable = _find_in_bins(name, bin_dirs)
        if executable is None or executable.is_symlink() or not executable.is_file():
            raise ToolchainProbeError(f"Managed media toolchain is missing: {name}")
        try:
            executable.resolve(strict=True).relative_to(payload_root)
        except (OSError, ValueError) as exc:
            raise ToolchainProbeError("Managed media executable escaped its payload") from exc
        executable.chmod(0o755)
        _run_checked(
            [str(codesign), "--remove-signature", str(executable)],
            cwd=payload,
            env=env,
            timeout=_PROBE_TIMEOUT_SECONDS,
            label=f"Remove invalid embedded signature from {name}",
        )
        _run_checked(
            [str(codesign), "--force", "--sign", "-", "--timestamp=none", str(executable)],
            cwd=payload,
            env=env,
            timeout=_PROBE_TIMEOUT_SECONDS,
            label=f"Ad-hoc sign managed {name}",
        )
        _run_checked(
            [str(codesign), "--verify", "--strict", "--verbose=2", str(executable)],
            cwd=payload,
            env=env,
            timeout=_PROBE_TIMEOUT_SECONDS,
            label=f"Verify managed {name} CodeDirectory",
        )


def _ffmpeg_media_capability(
    descriptor: ToolchainDescriptor,
    payload: Path,
    bin_dirs: tuple[Path, ...],
    *,
    env_override: Mapping[str, str] | None = None,
    resource_paths: Mapping[str, Path] | None = None,
) -> None:
    """Verify the filters and codecs used by the short-drama workflow."""
    executables: dict[str, Path] = {}
    for name in ("ffmpeg", "ffprobe"):
        executable = _find_in_bins(name, bin_dirs)
        if executable is None:
            raise ToolchainProbeError(f"Managed media toolchain is missing: {name}")
        executables[name] = executable
    cjk_font = _capability_resource_path(
        descriptor,
        payload,
        "noto-cjk-font",
        resource_paths,
    )
    if not cjk_font.is_file():
        raise ToolchainProbeError("Managed media toolchain is missing its pinned CJK font")
    if env_override is None:
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join(
            [*(str(path) for path in bin_dirs), env.get("PATH", "")]
        )
    else:
        env = dict(env_override)

    expected_match = re.match(r"(\d+(?:\.\d+){1,2})", descriptor.version)
    if expected_match is None:
        raise ToolchainProbeError("Managed FFmpeg catalog version is invalid")
    expected_version = expected_match.group(1)
    for name, executable in executables.items():
        version_result = _run_checked(
            [str(executable), "-version"],
            cwd=payload,
            env=env,
            timeout=_PROBE_TIMEOUT_SECONDS,
            label=f"{name} version and license inventory",
        )
        version_output = (version_result.stdout + version_result.stderr).decode(
            "utf-8", errors="replace"
        )
        if not re.search(
            rf"^{re.escape(name)} version (?:n)?{re.escape(expected_version)}(?:[-+\s]|$)",
            version_output,
            flags=re.IGNORECASE | re.MULTILINE,
        ):
            raise ToolchainProbeError(
                f"Managed {name} does not match catalog version {expected_version}"
            )
        lowered_version = version_output.casefold()
        if (
            "--enable-nonfree" in lowered_version
            or "not legally redistributable" in lowered_version
        ):
            raise ToolchainProbeError(f"Managed {name} is not legally redistributable")
        if name == "ffmpeg" and "--enable-gpl" not in lowered_version:
            raise ToolchainProbeError("Managed FFmpeg does not report its cataloged GPL build")

    filters_result = _run_checked(
        [str(executables["ffmpeg"]), "-hide_banner", "-filters"],
        cwd=payload,
        env=env,
        timeout=_PROBE_TIMEOUT_SECONDS,
        label="FFmpeg filter inventory",
    )
    filters = (filters_result.stdout + filters_result.stderr).decode(
        "utf-8", errors="replace"
    )
    required_filters = ("subtitles", "zoompan", "xfade")
    missing_filters = [
        name for name in required_filters if not re.search(rf"\b{name}\b", filters)
    ]
    if missing_filters:
        raise ToolchainProbeError(
            f"Managed FFmpeg is missing filters: {', '.join(missing_filters)}"
        )

    encoders_result = _run_checked(
        [str(executables["ffmpeg"]), "-hide_banner", "-encoders"],
        cwd=payload,
        env=env,
        timeout=_PROBE_TIMEOUT_SECONDS,
        label="FFmpeg encoder inventory",
    )
    encoders = (encoders_result.stdout + encoders_result.stderr).decode(
        "utf-8", errors="replace"
    )
    required_encoders = ("libx264", "aac")
    missing_encoders = [
        name for name in required_encoders if not re.search(rf"\b{name}\b", encoders)
    ]
    if missing_encoders:
        raise ToolchainProbeError(
            f"Managed FFmpeg is missing encoders: {', '.join(missing_encoders)}"
        )

    with tempfile.TemporaryDirectory(prefix="opensquilla-media-probe-") as temp_name:
        probe_root = Path(temp_name)
        fonts_root = probe_root / "fonts"
        fonts_root.mkdir()
        shutil.copyfile(cjk_font, fonts_root / cjk_font.name)
        (probe_root / "smoke.srt").write_text(
            "1\n00:00:00,000 --> 00:00:00,900\nOpenSquilla 中文烟测\n",
            encoding="utf-8",
        )
        output = probe_root / "smoke.mp4"
        _run_checked(
            [
                str(executables["ffmpeg"]),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x180:d=1:r=30",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x180:d=1:r=30",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:duration=1.5",
                "-filter_complex",
                (
                    "[0:v]zoompan=z='min(zoom+0.0015,1.05)':d=1:"
                    "s=320x180:fps=30,format=yuv420p,setsar=1[v0];"
                    "[1:v]format=yuv420p,setsar=1[v1];"
                    "[v0][v1]xfade=transition=fade:duration=0.25:offset=0.5,"
                    "subtitles=smoke.srt:fontsdir=fonts:"
                    "force_style='FontName=Noto Sans CJK SC'[video]"
                ),
                "-map",
                "[video]",
                "-map",
                "2:a:0",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                "-t",
                "1.25",
                "-y",
                str(output),
            ],
            cwd=probe_root,
            env=env,
            timeout=120,
            label="FFmpeg subtitle encode smoke test",
        )
        if not output.is_file() or output.stat().st_size == 0:
            raise ToolchainProbeError("FFmpeg smoke test did not produce a media file")
        probe_result = _run_checked(
            [
                str(executables["ffprobe"]),
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type:format=duration",
                "-of",
                "json",
                str(output),
            ],
            cwd=probe_root,
            env=env,
            timeout=_PROBE_TIMEOUT_SECONDS,
            label="FFprobe smoke test",
        )
        try:
            probe_data = json.loads(probe_result.stdout.decode("utf-8", errors="strict"))
            duration = float(probe_data["format"]["duration"])
            stream_types = {
                str(stream["codec_type"])
                for stream in probe_data["streams"]
                if isinstance(stream, dict) and "codec_type" in stream
            }
        except (KeyError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise ToolchainProbeError("FFprobe returned invalid smoke-test metadata") from exc
        if stream_types != {"audio", "video"}:
            raise ToolchainProbeError("FFmpeg smoke test did not contain audio and video streams")
        if not 0.5 <= duration <= 2.0:
            raise ToolchainProbeError("FFmpeg smoke-test duration was outside the expected range")


_POST_INSTALLERS: dict[
    str, Callable[[ToolchainDescriptor, Path, tuple[Path, ...]], None]
] = {
    "paper-capability": _paper_capability,
    "ffmpeg-media-capability": _ffmpeg_media_capability,
}


def _run_post_install(
    descriptor: ToolchainDescriptor,
    payload: Path,
    bin_dirs: tuple[Path, ...],
) -> None:
    if descriptor.post_install is None:
        return
    handler = _POST_INSTALLERS.get(descriptor.post_install)
    if handler is None:
        raise ToolchainProbeError(
            f"Unknown cataloged post-install strategy: {descriptor.post_install}"
        )
    handler(descriptor, payload, bin_dirs)


def _run_capability_only(
    descriptor: ToolchainDescriptor,
    payload: Path,
    bin_dirs: tuple[Path, ...],
    *,
    resource_paths: Mapping[str, Path] | None = None,
) -> None:
    if descriptor.post_install == "paper-capability":
        _paper_capability(
            descriptor,
            payload,
            bin_dirs,
            resource_paths=resource_paths,
        )
    elif descriptor.post_install == "ffmpeg-media-capability":
        _ffmpeg_media_capability(
            descriptor,
            payload,
            bin_dirs,
            resource_paths=resource_paths,
        )


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _receipt_payload(
    descriptor: ToolchainDescriptor,
    root: Path,
    package: Path,
    previous: dict[str, Any] | None,
    resources: dict[str, str],
) -> dict[str, Any]:
    package_relpath = package.relative_to(root).as_posix()
    receipt = asdict(
        ActivationReceipt(
            component_id=descriptor.component_id,
            version=descriptor.version,
            platform_key=descriptor.platform_key,
            sha256=descriptor.sha256 or "",
            install_backend=descriptor.install_backend,
            package_relpath=package_relpath,
            external_root=None,
            bin_relpaths=descriptor.bin_relpaths,
            resources=resources,
            activated_at_ms=int(time.time() * 1000),
            receipt_id=uuid.uuid4().hex,
        )
    )
    receipt["bin_relpaths"] = list(descriptor.bin_relpaths)
    receipt["previous"] = previous
    return receipt


def _external_receipt_payload(
    descriptor: ToolchainDescriptor,
    root: Path,
    package: Path,
    external_root: Path,
    previous: dict[str, Any] | None,
    resources: dict[str, str],
) -> dict[str, Any]:
    receipt = asdict(
        ActivationReceipt(
            component_id=descriptor.component_id,
            version=descriptor.version,
            platform_key=descriptor.platform_key,
            sha256="",
            install_backend=descriptor.install_backend,
            package_relpath=package.relative_to(root).as_posix(),
            external_root=str(external_root),
            bin_relpaths=descriptor.bin_relpaths,
            resources=resources,
            activated_at_ms=int(time.time() * 1000),
            receipt_id=uuid.uuid4().hex,
        )
    )
    receipt["bin_relpaths"] = list(descriptor.bin_relpaths)
    receipt["previous"] = previous
    return receipt


def _write_activation(root: Path, receipt: dict[str, Any]) -> None:
    component_id = str(receipt["component_id"])
    history_path = root / "receipts" / component_id / f"{receipt['receipt_id']}.json"
    _atomic_json(history_path, receipt)
    active_path = root / "active" / f"{component_id}.json"
    _atomic_json(active_path, receipt)
    invalidate_probe_cache(component_id)
    from opensquilla.skills.toolchains.runtime import invalidate_payload_validation_cache

    invalidate_payload_validation_cache(component_id, root=root)


def _valid_existing_package(package: Path, descriptor: ToolchainDescriptor) -> bool:
    marker = _read_json(package / ".opensquilla-toolchain.json")
    expected_assets = {
        asset.asset_id: asset.sha256 for asset in descriptor.auxiliary_assets
    }
    expected_asset_kinds = {
        asset.asset_id: "archive" if asset.archive_type is not None else "direct"
        for asset in descriptor.auxiliary_assets
    }
    expected_resources = _descriptor_resources(descriptor)
    valid_marker = bool(
        marker
        and marker.get("component_id") == descriptor.component_id
        and marker.get("version") == descriptor.version
        and marker.get("platform_key") == descriptor.platform_key
        and marker.get("sha256") == descriptor.sha256
        and marker.get("source") == descriptor.source
        and marker.get("install_backend") == descriptor.install_backend
        and marker.get("bin_relpaths", list(descriptor.bin_relpaths))
        == list(descriptor.bin_relpaths)
        and marker.get("resources", expected_resources) == expected_resources
        and marker.get("package_closure") == list(descriptor.package_closure)
        and marker.get("auxiliary_assets") == expected_assets
        and marker.get("auxiliary_asset_kinds", expected_asset_kinds)
        == expected_asset_kinds
    )
    if not valid_marker:
        return False
    if not package_payload_matches(package, descriptor, marker=marker):
        return False
    for asset in descriptor.auxiliary_assets:
        relative = _safe_archive_name(asset.destination)
        path = package.joinpath(*relative.parts)
        if not path.is_file():
            return False
        # Direct assets are installed byte-for-byte, so their catalog digest can
        # be checked again. Archived companions are verified before extraction
        # and may then be transformed (macOS binaries are ad-hoc signed); their
        # installed bytes are instead bound by the complete payload manifest.
        if asset.archive_type is None:
            digest = hashlib.sha256()
            try:
                with path.open("rb") as source:
                    while chunk := source.read(_COPY_CHUNK_SIZE):
                        digest.update(chunk)
            except OSError:
                return False
            if not hmac.compare_digest(digest.hexdigest(), asset.sha256):
                return False
    return True


def _backfill_package_marker_layout(
    package: Path,
    descriptor: ToolchainDescriptor,
) -> None:
    """Add install-time layout fields to a verified current-version marker.

    Early managed-toolchain builds already persisted a complete payload
    manifest but not the bin/resource layout needed after a later catalog
    changes its archive root. The marker itself is excluded from that manifest,
    so it can be upgraded atomically after the old marker and payload have both
    passed current-descriptor validation.
    """

    marker_path = package / ".opensquilla-toolchain.json"
    marker = _read_json(marker_path)
    if marker is None:
        raise ToolchainError("Managed package marker disappeared during validation")
    expected = {
        "bin_relpaths": list(descriptor.bin_relpaths),
        "resources": _descriptor_resources(descriptor),
        "auxiliary_asset_kinds": {
            asset.asset_id: "archive" if asset.archive_type is not None else "direct"
            for asset in descriptor.auxiliary_assets
        },
    }
    changed = False
    for key, value in expected.items():
        if key not in marker:
            marker[key] = value
            changed = True
    if changed:
        _atomic_json(marker_path, marker)


def _canonical_receipt_relative_path(value: object) -> str | None:
    """Return one canonical package-relative receipt path or fail closed."""

    if not isinstance(value, str):
        return None
    try:
        relative = _safe_archive_name(value)
    except UnsafeArchiveError:
        return None
    normalized = relative.as_posix()
    return normalized if normalized == value else None


def _historical_receipt_attests_layout(
    root: Path,
    receipt: Mapping[str, Any],
) -> bool:
    """Require the durable receipt copy to corroborate caller-owned layout fields."""

    component_id = receipt.get("component_id")
    receipt_id = receipt.get("receipt_id")
    if (
        not isinstance(component_id, str)
        or _SAFE_COMPONENT_RE.fullmatch(component_id) is None
        or not isinstance(receipt_id, str)
        or _SAFE_RECEIPT_ID_RE.fullmatch(receipt_id) is None
    ):
        return False
    historical = _read_json(root / "receipts" / component_id / f"{receipt_id}.json")
    if historical is None:
        return False
    attested_fields = (
        "component_id",
        "version",
        "platform_key",
        "sha256",
        "install_backend",
        "package_relpath",
        "external_root",
        "bin_relpaths",
        "resources",
        "activated_at_ms",
        "receipt_id",
    )
    return all(historical.get(field) == receipt.get(field) for field in attested_fields)


def _contained_existing_package_path(
    package: Path,
    relative_value: str,
) -> Path | None:
    """Resolve a receipt path only when it remains inside the verified package."""

    try:
        relative = _safe_archive_name(relative_value)
        package_root = package.resolve(strict=True)
        candidate = package.joinpath(*relative.parts)
        candidate.resolve(strict=True).relative_to(package_root)
    except (OSError, RuntimeError, UnsafeArchiveError, ValueError):
        return None
    return candidate


def backfill_legacy_historical_marker_layout(
    root: Path,
    package: Path,
    current: ToolchainDescriptor,
    receipt: Mapping[str, Any],
    marker: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Safely recover a pre-layout archive marker after a catalog upgrade.

    Early package markers omitted their bin/resource layout, while activation
    receipts already recorded it. Once the code-owned catalog advances, the
    current descriptor can no longer reconstruct those old paths. Recovery is
    therefore allowed only when the durable receipt copy corroborates every
    layout field and the marker's complete payload manifest still validates.
    Receipt paths are treated as untrusted until both checks pass.
    """

    layout_fields = ("bin_relpaths", "resources", "auxiliary_asset_kinds")
    missing_fields = tuple(field for field in layout_fields if field not in marker)
    if not missing_fields:
        return dict(marker)
    version = receipt.get("version")
    sha256 = receipt.get("sha256")
    package_relpath = receipt.get("package_relpath")
    marker_assets = marker.get("auxiliary_assets")
    manifest = marker.get("payload_manifest")
    if not (
        current.install_backend == "archive"
        and receipt.get("install_backend") == "archive"
        and receipt.get("external_root") is None
        and isinstance(version, str)
        and version != current.version
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", version) is not None
        and isinstance(sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", sha256) is not None
        and isinstance(package_relpath, str)
        and isinstance(marker_assets, dict)
        and marker.get("payload_manifest_version") == _PAYLOAD_MANIFEST_VERSION
        and isinstance(manifest, dict)
        and marker.get("component_id") == receipt.get("component_id") == current.component_id
        and marker.get("version") == version
        and marker.get("platform_key")
        == receipt.get("platform_key")
        == current.platform_key
        and marker.get("install_backend") == "archive"
        and marker.get("sha256") == sha256
        and _historical_receipt_attests_layout(root, receipt)
    ):
        return None

    expected_package_relpath = (
        Path("packages") / current.component_id / version / current.platform_key
    ).as_posix()
    if package_relpath != expected_package_relpath:
        return None
    expected_package = root.joinpath(*PurePosixPath(expected_package_relpath).parts)
    try:
        if package.resolve(strict=True) != expected_package.resolve(strict=True):
            return None
    except OSError:
        return None

    raw_bin_relpaths = receipt.get("bin_relpaths")
    if not isinstance(raw_bin_relpaths, list) or not raw_bin_relpaths:
        return None
    bin_relpaths: list[str] = []
    for raw_relative in raw_bin_relpaths:
        relative = _canonical_receipt_relative_path(raw_relative)
        if relative is None or relative in bin_relpaths:
            return None
        directory = _contained_existing_package_path(package, relative)
        if directory is None or not directory.is_dir():
            return None
        bin_relpaths.append(relative)

    raw_resources = receipt.get("resources")
    if not isinstance(raw_resources, dict) or set(raw_resources) != set(marker_assets):
        return None
    resources: dict[str, str] = {}
    auxiliary_asset_kinds: dict[str, str] = {}
    destinations: set[str] = set()
    for raw_asset_id, raw_destination in raw_resources.items():
        if (
            not isinstance(raw_asset_id, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", raw_asset_id) is None
        ):
            return None
        destination = _canonical_receipt_relative_path(raw_destination)
        expected_digest = marker_assets.get(raw_asset_id)
        manifest_entry = manifest.get(destination) if destination is not None else None
        if (
            destination is None
            or destination in destinations
            or not isinstance(expected_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
            or not isinstance(manifest_entry, dict)
            or manifest_entry.get("type") != "file"
            or re.fullmatch(r"[0-9a-f]{64}", str(manifest_entry.get("sha256", ""))) is None
        ):
            return None
        resource = _contained_existing_package_path(package, destination)
        if resource is None or resource.is_symlink() or not resource.is_file():
            return None
        resources[raw_asset_id] = destination
        destinations.add(destination)
        auxiliary_asset_kinds[raw_asset_id] = (
            "direct" if manifest_entry.get("sha256") == expected_digest else "archive"
        )

    recovered = {
        "bin_relpaths": bin_relpaths,
        "resources": resources,
        "auxiliary_asset_kinds": auxiliary_asset_kinds,
    }
    for field, value in recovered.items():
        if field in marker and marker.get(field) != value:
            return None

    historical_descriptor = replace(
        current,
        version=version,
        sha256=sha256,
        bin_relpaths=tuple(bin_relpaths),
    )
    if not package_payload_matches(package, historical_descriptor, marker=marker):
        return None

    upgraded = dict(marker)
    upgraded.update(recovered)
    try:
        _atomic_json(package / ".opensquilla-toolchain.json", upgraded)
    except OSError:
        return None
    return upgraded


def _manifest_entry(path: Path, *, package: Path) -> dict[str, Any]:
    if path.is_symlink():
        target = os.readlink(path)
        try:
            resolved_target = path.resolve(strict=True)
            resolved_target.relative_to(package.resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as exc:
            raise ToolchainError(
                f"Managed payload symlink escaped its package: {path.name}"
            ) from exc
        return {"type": "symlink", "target": target}
    if not path.is_file():
        raise ToolchainError(f"Managed payload entry is not a regular file: {path.name}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(_COPY_CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return {"type": "file", "size": size, "sha256": digest.hexdigest()}


def _manifest_tree_candidates(package: Path, root: Path) -> list[Path]:
    """Enumerate a payload tree without following symlinked directories."""

    try:
        relative_root = root.relative_to(package)
    except ValueError as exc:
        raise ToolchainError("Managed payload manifest escaped its package") from exc
    cursor = package
    for index, part in enumerate(relative_root.parts):
        cursor /= part
        if cursor.is_symlink():
            if index != len(relative_root.parts) - 1:
                raise ToolchainError("Managed payload path descends through a symlink")
            return [root]
    if root.is_symlink() or root.is_file():
        return [root]
    if not root.is_dir():
        raise ToolchainError("Managed payload manifest root is missing")

    candidates: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ToolchainError("Managed payload tree could not be enumerated") from exc
        for entry in entries:
            candidate = Path(entry.path)
            if candidate.name == ".opensquilla-toolchain.json" and candidate.parent == package:
                # The marker contains this manifest and is written only after the
                # payload digest has been computed. It must never digest itself.
                continue
            try:
                if entry.is_symlink():
                    candidates.append(candidate)
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(candidate)
                elif entry.is_file(follow_symlinks=False):
                    candidates.append(candidate)
                else:
                    raise ToolchainError(
                        f"Managed payload contains a special entry: {candidate.name}"
                    )
            except OSError as exc:
                raise ToolchainError("Managed payload entry could not be inspected") from exc
    return sorted(candidates, key=lambda path: path.as_posix())


def _payload_manifest(
    package: Path,
    descriptor: ToolchainDescriptor,
) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    if package.is_symlink() or not package.is_dir():
        raise ToolchainError("Managed payload package must be a real directory")
    roots: list[Path]
    if descriptor.install_backend == "archive":
        # Archive tools routinely load libraries, formats, scripts, TeX macro
        # trees, and other runtime data outside their bin directory. Manifest
        # the complete extracted payload so a valid executable cannot mask a
        # modified runtime dependency.
        roots = [package]
    else:
        # Homebrew owns and updates its external prefix. OpenSquilla can attest
        # only to the pinned auxiliary files stored in its own package.
        roots = [
            package.joinpath(*_safe_archive_name(asset.destination).parts)
            for asset in descriptor.auxiliary_assets
        ]
    for root in roots:
        for candidate in _manifest_tree_candidates(package, root):
            try:
                relative = candidate.relative_to(package).as_posix()
            except ValueError as exc:
                raise ToolchainError("Managed payload manifest escaped its package") from exc
            entries[relative] = _manifest_entry(candidate, package=package)
    return entries


def package_payload_matches(
    package: Path,
    descriptor: ToolchainDescriptor,
    *,
    marker: Mapping[str, Any] | None = None,
) -> bool:
    """Verify that managed executable/resource bytes match their install manifest."""

    current_marker = marker or _read_json(package / ".opensquilla-toolchain.json")
    if not current_marker or current_marker.get("payload_manifest_version") != (
        _PAYLOAD_MANIFEST_VERSION
    ):
        return False
    expected = current_marker.get("payload_manifest")
    if not isinstance(expected, dict):
        return False
    try:
        actual = _payload_manifest(package, descriptor)
        expected_bytes = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
        actual_bytes = json.dumps(actual, sort_keys=True, separators=(",", ":")).encode()
    except (OSError, ToolchainError, TypeError, ValueError):
        return False
    return hmac.compare_digest(expected_bytes, actual_bytes)


def _package_marker(
    descriptor: ToolchainDescriptor,
    package: Path | None = None,
) -> dict[str, Any]:
    resources = _descriptor_resources(descriptor)
    marker: dict[str, Any] = {
        "component_id": descriptor.component_id,
        "version": descriptor.version,
        "platform_key": descriptor.platform_key,
        "sha256": descriptor.sha256,
        "source": descriptor.source,
        "install_backend": descriptor.install_backend,
        # Persist the install-time runtime layout. Archive root names routinely
        # contain an upstream version/build identifier (notably FFmpeg on
        # Linux and Windows), so a future catalog cannot reconstruct an older
        # package's safe bin/resource paths from its current descriptor.
        "bin_relpaths": list(descriptor.bin_relpaths),
        "resources": resources,
        "package_closure": list(descriptor.package_closure),
        "auxiliary_assets": {
            asset.asset_id: asset.sha256 for asset in descriptor.auxiliary_assets
        },
        "auxiliary_asset_kinds": {
            asset.asset_id: "archive" if asset.archive_type is not None else "direct"
            for asset in descriptor.auxiliary_assets
        },
    }
    if package is not None:
        marker["payload_manifest_version"] = _PAYLOAD_MANIFEST_VERSION
        marker["payload_manifest"] = _payload_manifest(package, descriptor)
    return marker


def _component_package(root: Path, descriptor: ToolchainDescriptor) -> Path:
    return (
        root
        / "packages"
        / descriptor.component_id
        / descriptor.version
        / descriptor.platform_key
    )


def _descriptor_resources(descriptor: ToolchainDescriptor) -> dict[str, str]:
    return {
        asset.asset_id: _safe_archive_name(asset.destination).as_posix()
        for asset in descriptor.auxiliary_assets
    }


def _activation_receipt_from_mapping(value: Mapping[str, Any]) -> ActivationReceipt:
    return ActivationReceipt(
        component_id=str(value["component_id"]),
        version=str(value["version"]),
        platform_key=str(value["platform_key"]),
        sha256=str(value["sha256"]),
        install_backend=str(value["install_backend"]),
        package_relpath=(
            str(value["package_relpath"])
            if value.get("package_relpath") is not None
            else None
        ),
        external_root=(
            str(value["external_root"])
            if value.get("external_root") is not None
            else None
        ),
        bin_relpaths=tuple(str(item) for item in value["bin_relpaths"]),
        resources={str(key): str(item) for key, item in value["resources"].items()},
        activated_at_ms=int(value["activated_at_ms"]),
        receipt_id=str(value["receipt_id"]),
    )


def _current_activation_receipt(
    root: Path,
    descriptor: ToolchainDescriptor,
    package: Path,
    resources: Mapping[str, str],
    *,
    external_root: Path | None,
) -> ActivationReceipt | None:
    raw = _read_json(root / "active" / f"{descriptor.component_id}.json")
    if raw is None:
        return None
    expected_package = package.relative_to(root).as_posix()
    expected_external = str(external_root) if external_root is not None else None
    if not (
        raw.get("component_id") == descriptor.component_id
        and raw.get("version") == descriptor.version
        and raw.get("platform_key") == descriptor.platform_key
        and raw.get("sha256") == (descriptor.sha256 or "")
        and raw.get("install_backend") == descriptor.install_backend
        and raw.get("package_relpath") == expected_package
        and raw.get("external_root") == expected_external
        and raw.get("bin_relpaths") == list(descriptor.bin_relpaths)
        and raw.get("resources") == dict(resources)
        and isinstance(raw.get("activated_at_ms"), int)
        and not isinstance(raw.get("activated_at_ms"), bool)
        and isinstance(raw.get("receipt_id"), str)
        and bool(raw.get("receipt_id"))
    ):
        return None
    try:
        return _activation_receipt_from_mapping(raw)
    except (KeyError, TypeError, ValueError):
        return None


def _activate_existing_package(
    root: Path,
    descriptor: ToolchainDescriptor,
    package: Path,
    resources: dict[str, str],
    *,
    external_root: Path | None,
) -> ActivationReceipt:
    current = _current_activation_receipt(
        root,
        descriptor,
        package,
        resources,
        external_root=external_root,
    )
    if current is not None:
        return current

    active_path = root / "active" / f"{descriptor.component_id}.json"
    previous = _read_json(active_path)
    if previous is not None and previous.get("package_relpath") == (
        package.relative_to(root).as_posix()
    ):
        previous = None
    if external_root is None:
        receipt = _receipt_payload(descriptor, root, package, previous, resources)
    else:
        receipt = _external_receipt_payload(
            descriptor,
            root,
            package,
            external_root,
            previous,
            resources,
        )
    _write_activation(root, receipt)
    return _activation_receipt_from_mapping(receipt)


def _reuse_existing_package(
    root: Path,
    descriptor: ToolchainDescriptor,
    package: Path,
    *,
    bin_dirs: tuple[Path, ...] | None,
    probe_payload: Path,
    external_root: Path | None,
    progress_cb: ProgressCallback | None,
    probe_cb: ProbeCallback | None,
) -> ActivationReceipt | None:
    if not package.exists() or not _valid_existing_package(package, descriptor):
        return None
    _backfill_package_marker_layout(package, descriptor)
    try:
        selected_bin_dirs = bin_dirs or _find_payload_bins(package, descriptor)
        _run_capability_only(descriptor, package, selected_bin_dirs)
        _run_probes(descriptor, probe_payload, selected_bin_dirs, probe_cb)
    except (OSError, ToolchainError, subprocess.SubprocessError):
        return None
    total = descriptor.total_download_size or descriptor.size or 0
    _notify(progress_cb, total, total)
    return _activate_existing_package(
        root,
        descriptor,
        package,
        _descriptor_resources(descriptor),
        external_root=external_root,
    )


def _quarantine_package(package: Path) -> Path:
    quarantine = package.with_name(f".{package.name}.quarantine-{uuid.uuid4().hex}")
    os.replace(package, quarantine)
    return quarantine


def _restore_quarantined_package(package: Path, quarantine: Path) -> None:
    if package.exists():
        if package.is_dir() and not package.is_symlink():
            shutil.rmtree(package)
        else:
            package.unlink()
    os.replace(quarantine, package)


def _discard_quarantined_package(quarantine: Path | None) -> None:
    if quarantine is None:
        return
    if quarantine.is_dir() and not quarantine.is_symlink():
        shutil.rmtree(quarantine, ignore_errors=True)
    else:
        quarantine.unlink(missing_ok=True)


def install_component(
    component_id: str,
    progress_cb: ProgressCallback | None = None,
    *,
    root: Path | None = None,
    probe_cb: ProbeCallback | None = None,
) -> ActivationReceipt:
    """Serialize and install one code-owned component across threads/processes."""
    descriptor = registry.describe_component(component_id)
    if not _SAFE_COMPONENT_RE.fullmatch(descriptor.component_id):
        raise ToolchainError("Catalog component identifier is not path-safe")
    state_root = toolchains_root(root)
    _ensure_root(state_root)
    with _ComponentInstallLock(
        state_root,
        descriptor.component_id,
        _INSTALL_LOCK_TIMEOUT_SECONDS,
    ):
        return _install_component_unlocked(
            component_id,
            progress_cb,
            root=state_root,
            probe_cb=probe_cb,
        )


def _install_component_unlocked(
    component_id: str,
    progress_cb: ProgressCallback | None = None,
    *,
    root: Path | None = None,
    probe_cb: ProbeCallback | None = None,
) -> ActivationReceipt:
    """Download, verify, probe, and atomically activate a built-in component."""
    descriptor = registry.describe_component(component_id)
    if not descriptor.supported:
        raise UnsupportedToolchainError(
            descriptor.unsupported_reason or f"{component_id} is unsupported on this platform"
        )
    if not _SAFE_COMPONENT_RE.fullmatch(descriptor.component_id):
        raise ToolchainError("Catalog component identifier is not path-safe")
    if descriptor.install_backend == "brew":
        return _install_brew_component(
            descriptor,
            root=toolchains_root(root),
            progress_cb=progress_cb,
            probe_cb=probe_cb,
        )
    if descriptor.install_backend != "archive":
        raise UnsupportedToolchainError(
            f"Unsupported managed install backend: {descriptor.install_backend}"
        )
    if descriptor.archive_type not in {"tar.xz", "zip"}:
        raise UnsupportedToolchainError(
            f"The {descriptor.archive_type or 'unknown'} artifact format is not safely installable"
        )
    state_root = toolchains_root(root)
    _ensure_root(state_root)
    package = _component_package(state_root, descriptor)
    reused = _reuse_existing_package(
        state_root,
        descriptor,
        package,
        bin_dirs=None,
        probe_payload=package,
        external_root=None,
        progress_cb=progress_cb,
        probe_cb=probe_cb,
    )
    if reused is not None:
        return reused

    staging = Path(tempfile.mkdtemp(prefix=f"{component_id}-", dir=state_root / "staging"))
    try:
        archive = staging / "artifact"
        total_download = descriptor.total_download_size or descriptor.size or 0
        _download(
            descriptor,
            archive,
            progress_cb,
            progress_total=total_download,
        )
        payload = staging / "payload"
        _extract_archive(
            archive,
            payload,
            descriptor.archive_type,
            descriptor.size or 0,
        )
        _relocate_cataloged_archive_member(
            payload,
            member_name=descriptor.archive_member,
            destination_name=descriptor.archive_destination,
        )
        resources = _install_auxiliary_assets(
            descriptor,
            payload,
            progress_cb,
            progress_offset=descriptor.size or 0,
            progress_total=total_download,
        )
        if descriptor.archive_root:
            archive_root = payload / descriptor.archive_root
            if not archive_root.is_dir():
                raise UnsafeArchiveError(
                    f"Archive is missing its cataloged root: {descriptor.archive_root}"
                )
        bin_dirs = _find_payload_bins(payload, descriptor)
        _prepare_macos_media_payload(descriptor, payload, bin_dirs)
        _run_post_install(descriptor, payload, bin_dirs)
        _run_probes(descriptor, payload, bin_dirs, probe_cb)

        package.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        existing_valid = _valid_existing_package(package, descriptor) if package.exists() else False
        if existing_valid:
            try:
                existing_bins = _find_payload_bins(package, descriptor)
                _run_capability_only(descriptor, package, existing_bins)
                _run_probes(descriptor, package, existing_bins, probe_cb)
            except (OSError, ToolchainError, subprocess.SubprocessError):
                existing_valid = False
        quarantine = None
        try:
            if package.exists() and not existing_valid:
                quarantine = _quarantine_package(package)
            if not package.exists():
                _atomic_json(
                    payload / ".opensquilla-toolchain.json",
                    _package_marker(descriptor, payload),
                )
                os.replace(payload, package)

            active_path = state_root / "active" / f"{descriptor.component_id}.json"
            previous = None if quarantine is not None else _read_json(active_path)
            receipt = _receipt_payload(descriptor, state_root, package, previous, resources)
            _write_activation(state_root, receipt)
            _discard_quarantined_package(quarantine)
        except BaseException:
            if quarantine is not None:
                _restore_quarantined_package(package, quarantine)
            raise
        return ActivationReceipt(
            component_id=receipt["component_id"],
            version=receipt["version"],
            platform_key=receipt["platform_key"],
            sha256=receipt["sha256"],
            install_backend=receipt["install_backend"],
            package_relpath=receipt["package_relpath"],
            external_root=receipt["external_root"],
            bin_relpaths=tuple(receipt["bin_relpaths"]),
            resources=dict(receipt["resources"]),
            activated_at_ms=receipt["activated_at_ms"],
            receipt_id=receipt["receipt_id"],
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _brew_prefix(brew: str, formula: str) -> Path | None:
    try:
        completed = subprocess.run(
            [brew, "--prefix", formula],
            check=False,
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.decode("utf-8", errors="replace").strip()
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() and path.is_dir() else None


def _install_brew_component(
    descriptor: ToolchainDescriptor,
    *,
    root: Path,
    progress_cb: ProgressCallback | None,
    probe_cb: ProbeCallback | None,
) -> ActivationReceipt:
    formula = descriptor.brew_formula
    if not formula or not re.fullmatch(r"[a-z0-9][a-z0-9@+._-]{0,127}", formula):
        raise UnsupportedToolchainError("Managed Homebrew component has no safe formula")
    trusted_brew = registry.trusted_brew_executable()
    brew = str(trusted_brew) if trusted_brew is not None else None
    if brew is None:
        raise UnsupportedToolchainError(
            "Homebrew is required for the managed macOS FFmpeg toolchain."
        )
    _ensure_root(root)
    _notify(progress_cb, 0, 0)
    prefix = _brew_prefix(brew, formula)
    if prefix is None:
        _run_checked(
            [brew, "install", "--force-bottle", formula],
            cwd=root,
            env=os.environ,
            timeout=_POST_INSTALL_TIMEOUT_SECONDS,
            label=f"Homebrew {formula} bottle install",
        )
        prefix = _brew_prefix(brew, formula)
    if prefix is None:
        raise ToolchainProbeError(f"Homebrew did not expose a prefix for {formula}")
    bin_dirs = _find_payload_bins(prefix, descriptor)
    package = _component_package(root, descriptor)
    reused = _reuse_existing_package(
        root,
        descriptor,
        package,
        bin_dirs=bin_dirs,
        probe_payload=prefix,
        external_root=prefix,
        progress_cb=progress_cb,
        probe_cb=probe_cb,
    )
    if reused is not None:
        return reused

    staging = Path(
        tempfile.mkdtemp(prefix=f"{descriptor.component_id}-", dir=root / "staging")
    )
    try:
        payload = staging / "payload"
        payload.mkdir(mode=0o700)
        auxiliary_total = sum(asset.size for asset in descriptor.auxiliary_assets)
        resources = _install_auxiliary_assets(
            descriptor,
            payload,
            progress_cb,
            progress_offset=0,
            progress_total=auxiliary_total,
        )
        _run_post_install(descriptor, payload, bin_dirs)
        _run_probes(descriptor, prefix, bin_dirs, probe_cb)
        package.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        existing_valid = _valid_existing_package(package, descriptor) if package.exists() else False
        if existing_valid:
            try:
                _run_capability_only(descriptor, package, bin_dirs)
                _run_probes(descriptor, prefix, bin_dirs, probe_cb)
            except (OSError, ToolchainError, subprocess.SubprocessError):
                existing_valid = False
        quarantine = None
        try:
            if package.exists() and not existing_valid:
                quarantine = _quarantine_package(package)
            if not package.exists():
                _atomic_json(
                    payload / ".opensquilla-toolchain.json",
                    _package_marker(descriptor, payload),
                )
                os.replace(payload, package)
            previous = (
                None
                if quarantine is not None
                else _read_json(root / "active" / f"{descriptor.component_id}.json")
            )
            receipt = _external_receipt_payload(
                descriptor,
                root,
                package,
                prefix,
                previous,
                resources,
            )
            _write_activation(root, receipt)
            _discard_quarantined_package(quarantine)
        except BaseException:
            if quarantine is not None:
                _restore_quarantined_package(package, quarantine)
            raise
        return ActivationReceipt(
            component_id=receipt["component_id"],
            version=receipt["version"],
            platform_key=receipt["platform_key"],
            sha256=receipt["sha256"],
            install_backend=receipt["install_backend"],
            package_relpath=receipt["package_relpath"],
            external_root=receipt["external_root"],
            bin_relpaths=tuple(receipt["bin_relpaths"]),
            resources=dict(receipt["resources"]),
            activated_at_ms=receipt["activated_at_ms"],
            receipt_id=receipt["receipt_id"],
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def rollback_component(component_id: str, *, root: Path | None = None) -> bool:
    """Atomically restore the activation that preceded the current receipt."""
    # This validates the identifier against the code-owned catalog even when no
    # installation exists, preventing receipt filenames from being caller-owned.
    descriptor = registry.describe_component(component_id)
    if not _SAFE_COMPONENT_RE.fullmatch(descriptor.component_id):
        raise ToolchainError("Catalog component identifier is not path-safe")
    state_root = toolchains_root(root)
    _ensure_root(state_root)
    with _ComponentInstallLock(
        state_root,
        descriptor.component_id,
        _INSTALL_LOCK_TIMEOUT_SECONDS,
    ):
        return _rollback_component_unlocked(descriptor, root=state_root)


def _rollback_component_unlocked(
    descriptor: ToolchainDescriptor,
    *,
    root: Path,
) -> bool:
    """Restore one activation while the caller holds its component mutation lock."""
    current = _read_json(root / "active" / f"{descriptor.component_id}.json")
    if current is None or not isinstance(current.get("previous"), dict):
        return False
    previous = dict(current["previous"])
    if not (
        previous.get("component_id") == descriptor.component_id
        and previous.get("platform_key") == descriptor.platform_key
        and previous.get("install_backend") == descriptor.install_backend
        and isinstance(previous.get("version"), str)
    ):
        raise ToolchainError("The previous managed activation is incompatible")
    if (
        descriptor.install_backend == "brew"
        and previous.get("version") != descriptor.version
    ):
        # Homebrew receipts point at a live formula prefix rather than an
        # OpenSquilla-owned version snapshot. Activating an older receipt would
        # claim to restore old binaries while continuing to execute the current
        # external prefix, so reject the rollback before mutating active state.
        raise ToolchainError(
            "Historical Homebrew activations cannot be restored from a live prefix"
        )
    package = _package_from_receipt(root, previous)
    if not package.is_dir() or not (package / ".opensquilla-toolchain.json").is_file():
        raise ToolchainError("The previous managed package is no longer available")
    external = previous.get("external_root")
    if external is not None:
        if not isinstance(external, str):
            raise ToolchainError("The previous external activation root is invalid")
        external_path = Path(external)
        if not external_path.is_absolute() or not external_path.is_dir():
            raise ToolchainError("The previous external activation root is unavailable")

    # Validate the exact candidate before changing active state. In particular,
    # a historical archive may have versioned bin/resource paths that differ
    # from the current catalog; its install-time marker must bind those paths,
    # its complete payload manifest must still match, and its native capability
    # probes must still succeed. A failed rollback therefore leaves the current
    # activation untouched instead of returning a false success.
    from opensquilla.skills.toolchains.runtime import _validated_activation_receipt

    activation = _validated_activation_receipt(
        root,
        descriptor,
        previous,
        verify_payload=True,
    )
    if activation is None:
        raise ToolchainError("The previous managed activation failed validation")
    resource_paths = {
        asset_id: activation.package.joinpath(*_safe_archive_name(relative).parts)
        for asset_id, relative in activation.resources.items()
    }
    _run_capability_only(
        activation.descriptor,
        activation.package,
        activation.bin_dirs,
        resource_paths=resource_paths,
    )
    _run_probes(
        activation.descriptor,
        activation.package,
        activation.bin_dirs,
        None,
    )

    # Retain a one-step inverse pointer, making rollback itself recoverable.
    inverse = dict(current)
    inverse["previous"] = None
    previous["previous"] = inverse
    previous["activated_at_ms"] = int(time.time() * 1000)
    previous["receipt_id"] = uuid.uuid4().hex
    previous["rolled_back_from"] = current.get("receipt_id")
    _write_activation(root, previous)
    return True


def _package_from_receipt(root: Path, receipt: Mapping[str, Any]) -> Path:
    raw = receipt.get("package_relpath")
    if not isinstance(raw, str):
        raise ToolchainError("Managed activation receipt has no package path")
    relative = _safe_archive_name(raw)
    package = root.joinpath(*relative.parts)
    try:
        package.relative_to(root)
    except ValueError as exc:
        raise ToolchainError("Managed activation receipt escaped the state root") from exc
    return package


def invalidate_probe_cache(component_id: str | None = None) -> None:
    """Invalidate cached effective-capability reports."""
    with _probe_cache_lock:
        if component_id is None:
            _probe_cache.clear()
            return
        prefix = f"{component_id}:"
        for key in tuple(_probe_cache):
            if key.startswith(prefix):
                _probe_cache.pop(key, None)


def _probe_cache_key(
    descriptor: ToolchainDescriptor,
    state_root: Path,
    env: Mapping[str, str],
) -> str:
    active_path = state_root / "active" / f"{descriptor.component_id}.json"
    try:
        active_digest = hashlib.sha256(active_path.read_bytes()).hexdigest()
    except OSError:
        active_digest = "none"
    material = "\x00".join(
        (
            descriptor.component_id,
            descriptor.version,
            descriptor.platform_key,
            env.get("PATH", ""),
            env.get("OPENSQUILLA_MEDIA_FONTS_DIR", ""),
            env.get("OSFONTDIR", ""),
            active_digest,
        )
    )
    return f"{descriptor.component_id}:{hashlib.sha256(material.encode()).hexdigest()}"


def _cached_probe(key: str) -> CapabilityReport | None:
    with _probe_cache_lock:
        cached = _probe_cache.get(key)
        if cached is None:
            return None
        created, report = cached
        if time.monotonic() - created > _PROBE_CACHE_TTL_SECONDS:
            _probe_cache.pop(key, None)
            return None
        return report


def _store_probe(key: str, report: CapabilityReport) -> CapabilityReport:
    with _probe_cache_lock:
        _probe_cache[key] = (time.monotonic(), report)
    return report


def probe_component(
    component_id: str,
    *,
    root: Path | None = None,
    base_env: Mapping[str, str] | None = None,
) -> CapabilityReport:
    """Probe the effective managed-first runtime used by skills.

    The result is readiness-safe: expected missing-tool and capability failures
    are returned as ``ready=False`` rather than raised. Unknown component ids
    still fail closed through the code-owned registry.
    """
    from opensquilla.skills.toolchains.runtime import (
        MEDIA_FONTS_ENV,
        PAPER_FONTS_ENV,
        _validated_activation,
        managed_env,
    )

    current_descriptor = registry.describe_component(component_id)
    state_root = toolchains_root(root)
    activation = _validated_activation(
        state_root,
        current_descriptor,
        verify_payload=True,
    )
    descriptor = activation.descriptor if activation is not None else current_descriptor
    checked_at_ms = int(time.time() * 1000)
    if (
        activation is not None
        and descriptor.version == current_descriptor.version
        and descriptor.install_backend == "archive"
    ):
        try:
            _backfill_package_marker_layout(activation.package, current_descriptor)
        except (OSError, ToolchainError) as exc:
            return CapabilityReport(
                component_id=component_id,
                version=descriptor.version,
                platform_key=descriptor.platform_key,
                supported=descriptor.supported,
                ready=False,
                reason=f"Managed package metadata upgrade failed: {exc}",
                binaries={},
                resources={},
                checked_at_ms=checked_at_ms,
            )
    env = managed_env(base_env, root=state_root)
    cache_key = _probe_cache_key(descriptor, state_root, env)
    cached = _cached_probe(cache_key)
    if cached is not None:
        return cached

    required_names = {command[0] for command in descriptor.probe_commands if command}
    if descriptor.post_install == "paper-capability":
        required_names.add("kpsewhich")
    binaries: dict[str, str] = {}
    missing: list[str] = []
    effective_path = env.get("PATH", "")
    for name in sorted(required_names):
        resolved = shutil.which(name, path=effective_path)
        if resolved is None:
            missing.append(name)
        else:
            binaries[name] = resolved

    resources: dict[str, str] = {}
    resource_paths: dict[str, Path] = {}
    payload = Path.cwd()
    if activation is not None:
        payload = activation.package
        for asset_id, relative in activation.resources.items():
            resource = activation.package.joinpath(*_safe_archive_name(relative).parts)
            resources[asset_id] = str(resource)
            resource_paths[asset_id] = resource
        if (
            descriptor.post_install in {"paper-capability", "ffmpeg-media-capability"}
            and "noto-cjk-font" not in resource_paths
        ):
            missing.append("noto-cjk-font")
    elif descriptor.post_install == "paper-capability":
        fonts_dir_raw = env.get(PAPER_FONTS_ENV, "").split(os.pathsep, maxsplit=1)[0]
        fonts_dir = Path(fonts_dir_raw) if fonts_dir_raw else None
        font = fonts_dir / "NotoSansCJK-Regular.ttc" if fonts_dir is not None else None
        if font is None or not font.is_file():
            missing.append("noto-cjk-font")
        else:
            resources["noto-cjk-font"] = str(font)
            resource_paths["noto-cjk-font"] = font
            payload = font.parent
    elif descriptor.post_install == "ffmpeg-media-capability":
        fonts_dir_raw = env.get(MEDIA_FONTS_ENV, "")
        fonts_dir = Path(fonts_dir_raw) if fonts_dir_raw else None
        font = fonts_dir / "NotoSansCJK-Regular.ttc" if fonts_dir is not None else None
        if font is None or not font.is_file():
            missing.append("noto-cjk-font")
        else:
            resources["noto-cjk-font"] = str(font)
            resource_paths["noto-cjk-font"] = font
            payload = font.parent

    if missing:
        reason = f"Missing runtime capabilities: {', '.join(sorted(missing))}"
        if not descriptor.supported and descriptor.unsupported_reason:
            reason = f"{reason}. {descriptor.unsupported_reason}"
        return _store_probe(
            cache_key,
            CapabilityReport(
                component_id=component_id,
                version=descriptor.version,
                platform_key=descriptor.platform_key,
                supported=descriptor.supported,
                ready=False,
                reason=reason,
                binaries=binaries,
                resources=resources,
                checked_at_ms=checked_at_ms,
            ),
        )

    path_dirs = tuple(Path(item) for item in effective_path.split(os.pathsep) if item)
    try:
        for command in descriptor.probe_commands:
            if not command:
                continue
            _run_checked(
                [binaries[command[0]], *command[1:]],
                cwd=payload,
                env=env,
                timeout=_PROBE_TIMEOUT_SECONDS,
                label=f"{command[0]} runtime probe",
            )
        if descriptor.post_install == "paper-capability":
            _paper_capability(
                descriptor,
                payload,
                path_dirs,
                env_override=env,
                resource_paths=resource_paths,
            )
        elif descriptor.post_install == "ffmpeg-media-capability":
            _ffmpeg_media_capability(
                descriptor,
                payload,
                path_dirs,
                env_override=env,
                resource_paths=resource_paths,
            )
    except (OSError, ToolchainError, subprocess.SubprocessError) as exc:
        report = CapabilityReport(
            component_id=component_id,
            version=descriptor.version,
            platform_key=descriptor.platform_key,
            supported=descriptor.supported,
            ready=False,
            reason=str(exc) or exc.__class__.__name__,
            binaries=binaries,
            resources=resources,
            checked_at_ms=checked_at_ms,
        )
        return _store_probe(cache_key, report)

    return _store_probe(
        cache_key,
        CapabilityReport(
            component_id=component_id,
            version=descriptor.version,
            platform_key=descriptor.platform_key,
            supported=descriptor.supported,
            ready=True,
            reason="ready",
            binaries=binaries,
            resources=resources,
            checked_at_ms=checked_at_ms,
        ),
    )
