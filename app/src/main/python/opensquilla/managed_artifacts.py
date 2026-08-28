"""Package-neutral locking and safe extraction for managed artifacts."""

from __future__ import annotations

import lzma
import os
import re
import stat
import tarfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
_MAX_EXPANSION_RATIO = 100
_COPY_CHUNK_SIZE = 1024 * 1024
_component_thread_locks: dict[str, threading.Lock] = {}
_component_thread_locks_guard = threading.Lock()


class ManagedArtifactError(RuntimeError):
    """Base error for package-neutral managed artifact operations."""


class DownloadVerificationError(ManagedArtifactError):
    """Raised when verified archive bytes are unavailable or changed."""


class UnsafeArchiveError(ManagedArtifactError):
    """Raised when an archive violates extraction safety limits."""


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


class ManagedArtifactInstallLock:
    """Serialize one managed component across threads and Gateway processes."""

    def __init__(self, root: Path, component_id: str, timeout: float) -> None:
        self.root = root
        self.component_id = component_id
        self.timeout = max(0.0, timeout)
        self.handle: Any = None
        self.thread_lock: threading.Lock | None = None

    def __enter__(self) -> ManagedArtifactInstallLock:
        key = f"{self.root.absolute()}::{self.component_id}"
        with _component_thread_locks_guard:
            self.thread_lock = _component_thread_locks.setdefault(key, threading.Lock())
        if not self.thread_lock.acquire(timeout=self.timeout):
            raise ManagedArtifactError(
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
                    raise ManagedArtifactError(
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
        finally:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            if self.thread_lock is not None:
                self.thread_lock.release()
                self.thread_lock = None


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


def _extraction_limit(compressed_size: int, max_extracted_bytes: int | None) -> int:
    ratio_limit = max(compressed_size, compressed_size * _MAX_EXPANSION_RATIO)
    limit = min(_MAX_EXTRACTED_BYTES, ratio_limit)
    if max_extracted_bytes is not None:
        if max_extracted_bytes <= 0:
            raise UnsafeArchiveError("Archive extracted-size limit must be positive")
        limit = min(limit, max_extracted_bytes)
    return limit


def _claim_destination(
    destination_root: Path,
    relative: PurePosixPath,
    seen: set[str],
    *,
    case_sensitive_paths: bool = False,
) -> Path:
    key = relative.as_posix()
    if not case_sensitive_paths:
        key = key.casefold()
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
                raise UnsafeArchiveError(
                    f"Archive link target escapes its root: {linkname!r}"
                )
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
    max_extracted_bytes: int | None,
    *,
    case_sensitive_paths: bool = False,
) -> tuple[
    list[tarfile.TarInfo],
    dict[PurePosixPath, PurePosixPath],
]:
    total_size = 0
    seen: set[str] = set()
    limit = _extraction_limit(compressed_size, max_extracted_bytes)
    members: list[tarfile.TarInfo] = []
    paths: dict[PurePosixPath, tarfile.TarInfo] = {}
    with tarfile.open(archive, mode="r|xz") as source:
        for member in source:
            if len(members) >= _MAX_ARCHIVE_MEMBERS:
                raise UnsafeArchiveError("Archive contains too many entries")
            relative = _safe_archive_name(member.name)
            _claim_destination(
                destination,
                relative,
                seen,
                case_sensitive_paths=case_sensitive_paths,
            )
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
    for relative, member in paths.items():
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
    return members, final_targets


def _extract_tar_xz(
    archive: Path,
    destination: Path,
    compressed_size: int,
    max_extracted_bytes: int | None,
    *,
    case_sensitive_paths: bool = False,
) -> None:
    members, final_targets = _validate_tar_members(
        archive,
        destination,
        compressed_size,
        max_extracted_bytes,
        case_sensitive_paths=case_sensitive_paths,
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


def _extract_zip(
    archive: Path,
    destination: Path,
    compressed_size: int,
    max_extracted_bytes: int | None,
) -> None:
    total_size = 0
    seen: set[str] = set()
    limit = _extraction_limit(compressed_size, max_extracted_bytes)
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
    max_extracted_bytes: int | None = None,
    *,
    case_sensitive_paths: bool = False,
) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    if archive_type == "tar.xz":
        _extract_tar_xz(
            archive,
            destination,
            compressed_size,
            max_extracted_bytes,
            case_sensitive_paths=case_sensitive_paths,
        )
        return
    if archive_type == "zip":
        _extract_zip(
            archive,
            destination,
            compressed_size,
            max_extracted_bytes,
        )
        return
    raise ManagedArtifactError(f"Unsupported managed archive type: {archive_type}")


def extract_managed_archive(
    archive: str | Path,
    destination: str | Path,
    *,
    archive_type: str,
    compressed_size: int,
    max_extracted_bytes: int | None = None,
    case_sensitive_paths: bool = False,
) -> None:
    """Safely extract a verified Runtime Pack without sandbox or skill imports."""

    archive_path = Path(archive)
    try:
        actual_size = archive_path.stat().st_size
    except OSError as exc:
        raise DownloadVerificationError("Managed archive is unavailable") from exc
    if actual_size != compressed_size:
        raise DownloadVerificationError(
            f"Managed archive size mismatch: expected {compressed_size}, received {actual_size}"
        )
    try:
        _extract_archive(
            archive_path,
            Path(destination),
            archive_type,
            compressed_size,
            max_extracted_bytes,
            case_sensitive_paths=case_sensitive_paths,
        )
    except (EOFError, lzma.LZMAError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise UnsafeArchiveError("Managed archive is malformed or truncated") from exc


__all__ = [
    "DownloadVerificationError",
    "ManagedArtifactError",
    "ManagedArtifactInstallLock",
    "UnsafeArchiveError",
    "_extract_archive",
    "_safe_archive_name",
    "extract_managed_archive",
]
