"""Persistent Runtime Pack download, validation, activation, and removal."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import locale
import logging
import os
import platform as platform_module
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from opensquilla.managed_artifacts import (
    DownloadVerificationError,
    ManagedArtifactError,
    ManagedArtifactInstallLock,
    UnsafeArchiveError,
    extract_managed_archive,
)
from opensquilla.paths import state_dir as default_state_dir
from opensquilla.runtime_packs.catalog import (
    RuntimePackCatalog,
    RuntimePackDescriptor,
    component_ids,
    load_default_catalog,
)
from opensquilla.runtime_packs.models import (
    RuntimeAvailability,
    RuntimeComponentStatus,
    RuntimeOperation,
    RuntimeOperationKind,
    RuntimeOperationState,
    RuntimePackStatus,
    RuntimeSource,
)
from opensquilla.runtime_packs.models import (
    RuntimeError as RuntimePublicError,
)
from opensquilla.runtime_target import runtime_target

_LAYOUT_VERSION = "v1"
_OSS_BASE = "https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/runtime-packs"
_GITHUB_BASE = "https://github.com/opensquilla/runtime-packs/releases/download"
_COPY_CHUNK_SIZE = 1024 * 1024
_INSTALL_LOCK_TIMEOUT_SECONDS = 900.0
_PROBE_TIMEOUT_SECONDS = 30.0
_DISK_SAFETY_BYTES = 64 * 1024**2
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
_SAFE_RELATIVE_RE = re.compile(r"^[^\x00]+$")
_PACK_MARKER = ".opensquilla-runtime-pack.json"
_ARCHIVE_ROOT_ENTRIES = frozenset(
    {"pack-manifest.json", "payload", "licenses", "SBOM.spdx.json"}
)
_REQUIRED_EXECUTABLES: Mapping[str, frozenset[str]] = {
    "python": frozenset({"python"}),
    "node": frozenset({"node", "npm", "npx"}),
    "gitBash": frozenset({"git", "bash"}),
}
_ACTIVE_STATES = frozenset(
    {
        RuntimeOperationState.QUEUED,
        RuntimeOperationState.DOWNLOADING,
        RuntimeOperationState.VERIFYING,
        RuntimeOperationState.EXTRACTING,
        RuntimeOperationState.PROBING,
        RuntimeOperationState.ACTIVATING,
        RuntimeOperationState.CANCELLING,
        RuntimeOperationState.REMOVING,
    }
)
_INTEGRITY_CACHE_TTL_SECONDS = 30.0
_SAFE_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_STATE_JSON_BYTES = 64 * 1024**2
_WINDOWS_ATOMIC_REPLACE_RETRY_DELAYS_SECONDS = (0.02, 0.05, 0.1, 0.2)
_WINDOWS_TRANSIENT_REPLACE_ERRORS = frozenset({5, 32, 33})
_configured_state_dir: ContextVar[Path | None] = ContextVar(
    "opensquilla_runtime_pack_state_dir",
    default=None,
)

UrlOpen = Callable[..., Any]
_LOG = logging.getLogger(__name__)


class RuntimePackError(RuntimeError):
    """Base error for Runtime Pack operations."""


class RuntimePackUnavailableError(RuntimePackError):
    """Raised when the immutable catalog does not support an operation."""


class RuntimePackCancelledError(RuntimePackError):
    """Raised internally when a user cancels an operation."""


class RuntimePackDownloadError(RuntimePackError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        source: RuntimeSource,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.source = source


@dataclass(frozen=True)
class PackLayout:
    component_id: str
    target: str
    version: str
    bin_dirs: tuple[str, ...]
    executables: Mapping[str, str]


@dataclass(frozen=True)
class ActiveRuntime:
    component_id: str
    version: str
    target: str
    package: Path
    bin_dirs: tuple[Path, ...]
    executables: Mapping[str, Path]
    installed_bytes: int


@contextmanager
def runtime_pack_state_scope(configured_state_dir: str | Path | None):
    """Bind Runtime Pack storage to a Gateway/task configured state directory."""

    if configured_state_dir is None or not str(configured_state_dir).strip():
        yield
        return
    token = _configured_state_dir.set(Path(configured_state_dir).expanduser())
    try:
        yield
    finally:
        _configured_state_dir.reset(token)


def runtime_packs_root(configured_state_dir: str | Path | None = None) -> Path:
    """Return the fixed, schema-versioned Runtime Pack state root."""

    explicit = os.environ.get("OPENSQUILLA_RUNTIME_PACKS_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser() / _LAYOUT_VERSION
    if configured_state_dir is not None and str(configured_state_dir).strip():
        return Path(configured_state_dir).expanduser() / "runtime-packs" / _LAYOUT_VERSION
    scoped = _configured_state_dir.get()
    if scoped is not None:
        return scoped / "runtime-packs" / _LAYOUT_VERSION
    gateway_state = os.environ.get("OPENSQUILLA_GATEWAY_STATE_DIR", "").strip()
    if gateway_state:
        return Path(gateway_state).expanduser() / "runtime-packs" / _LAYOUT_VERSION
    return default_state_dir("runtime-packs", _LAYOUT_VERSION)


def _ensure_root(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise RuntimePackError("Runtime Pack root must be a real directory")
    try:
        root.chmod(0o700)
    except OSError:
        pass
    for name in (
        "packages",
        "active",
        "receipts",
        "downloads",
        "operations",
        "staging",
        "trash",
        "locks",
    ):
        directory = root / name
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimePackError("Runtime Pack storage contains an unsafe directory")


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
        _replace_atomic_state(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _replace_atomic_state(source: Path, destination: Path) -> None:
    """Publish state after transient Windows readers release the destination."""

    for delay in (*_WINDOWS_ATOMIC_REPLACE_RETRY_DELAYS_SECONDS, None):
        try:
            os.replace(source, destination)
            return
        except PermissionError as error:
            if (
                os.name != "nt"
                or getattr(error, "winerror", None) not in _WINDOWS_TRANSIENT_REPLACE_ERRORS
                or delay is None
            ):
                raise
            time.sleep(delay)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _MAX_STATE_JSON_BYTES
        ):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_relative(value: Any, field: str, *, allow_dot: bool = False) -> str:
    text = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(text)
    if (
        not text
        or not _SAFE_RELATIVE_RE.fullmatch(text)
        or path.is_absolute()
        or ".." in path.parts
        or (not allow_dot and path == PurePosixPath("."))
    ):
        raise RuntimePackError(f"{field} must be a safe relative path")
    return path.as_posix()


def _contained_path(root: Path, relative: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        candidate.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise RuntimePackError("Runtime Pack path escaped its package") from exc
    return candidate


def _real_path_within(root: Path, candidate: Path, *, directory: bool) -> bool:
    try:
        root_absolute = root.absolute()
        candidate_absolute = candidate.absolute()
        candidate_absolute.relative_to(root_absolute)
        root_resolved = root_absolute.resolve(strict=True)
        candidate_resolved = candidate_absolute.resolve(strict=True)
        candidate_resolved.relative_to(root_resolved)
        current = candidate_absolute
        while current != root_absolute:
            if current.is_symlink():
                return False
            current = current.parent
        return candidate_absolute.is_dir() if directory else candidate_absolute.is_file()
    except (OSError, RuntimeError, ValueError):
        return False


def _ensure_package_parent(root: Path, descriptor: RuntimePackDescriptor) -> Path:
    parent = root / "packages"
    if not _real_path_within(root, parent, directory=True):
        raise RuntimePackError("Runtime Pack package storage is unsafe")
    for name in (descriptor.component_id, descriptor.version):
        parent = parent / name
        parent.mkdir(mode=0o700, exist_ok=True)
        if not _real_path_within(root, parent, directory=True):
            raise RuntimePackError("Runtime Pack package storage is unsafe")
    package = parent / descriptor.target
    if package.exists() and not _real_path_within(root, package, directory=True):
        raise RuntimePackError("Runtime Pack package target is unsafe")
    return package


def _host_is_supported_linux_libc() -> bool:
    if not sys_platform_is_linux():
        return True
    if Path("/etc/alpine-release").exists():
        return False
    libc_name, _version = platform_module.libc_ver()
    normalized = libc_name.casefold()
    if "glibc" in normalized or "gnu" in normalized:
        return True
    try:
        configured = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (AttributeError, OSError, ValueError):
        configured = ""
    return configured.casefold().startswith("glibc ")


def sys_platform_is_linux() -> bool:
    import sys

    return sys.platform.startswith("linux")


def _locale_prefers_china() -> bool:
    values = [
        os.environ.get("LC_ALL", ""),
        os.environ.get("LC_MESSAGES", ""),
        os.environ.get("LANGUAGE", ""),
        os.environ.get("LANG", ""),
    ]
    try:
        current_locale = locale.getlocale()[0]
    except (ValueError, TypeError):
        current_locale = None
    if current_locale:
        values.append(current_locale)
    normalized = " ".join(values).replace("-", "_").casefold()
    return any(marker in normalized for marker in ("zh_cn", "zh_hans", "zh_sg"))


def _configured_source_order(
    last_success: RuntimeSource | None = None,
) -> tuple[RuntimeSource, ...]:
    override = os.environ.get("OPENSQUILLA_RUNTIME_PACK_SOURCE_ORDER", "").strip()
    if override:
        try:
            parsed = tuple(RuntimeSource(item.strip()) for item in override.split(","))
        except ValueError:
            parsed = ()
        if len(parsed) == 2 and len(set(parsed)) == 2:
            return parsed
    default = (
        (RuntimeSource.OSS, RuntimeSource.GITHUB)
        if _locale_prefers_china()
        else (RuntimeSource.GITHUB, RuntimeSource.OSS)
    )
    if last_success in default:
        return (last_success, *(source for source in default if source is not last_success))
    return default


def _default_source_bases() -> Mapping[RuntimeSource, str]:
    values = {
        RuntimeSource.OSS: os.environ.get(
            "OPENSQUILLA_RUNTIME_PACK_OSS_BASE", _OSS_BASE
        ).rstrip("/"),
        RuntimeSource.GITHUB: os.environ.get(
            "OPENSQUILLA_RUNTIME_PACK_GITHUB_BASE", _GITHUB_BASE
        ).rstrip("/"),
    }
    if any(not value.startswith("https://") for value in values.values()):
        raise RuntimePackUnavailableError("Runtime Pack source overrides must use HTTPS")
    return values


def _source_url(
    base: str,
    catalog: RuntimePackCatalog,
    descriptor: RuntimePackDescriptor,
) -> str:
    return f"{base}/{catalog.release_tag}/{descriptor.asset}"


def _verify_archive(path: Path, descriptor: RuntimePackDescriptor) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DownloadVerificationError("Runtime Pack download is missing") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size != descriptor.size_bytes
    ):
        raise DownloadVerificationError(
            "Runtime Pack download is not a regular file with the pinned size"
        )
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor_fd = os.open(path, flags)
    except OSError as exc:
        raise DownloadVerificationError("Runtime Pack download could not be opened safely") from exc
    with os.fdopen(descriptor_fd, "rb") as source:
        opened = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size != descriptor.size_bytes
        ):
            raise DownloadVerificationError("Runtime Pack download changed during verification")
        while chunk := source.read(_COPY_CHUNK_SIZE):
            digest.update(chunk)
    if not hmac.compare_digest(digest.hexdigest(), descriptor.sha256):
        raise DownloadVerificationError("Runtime Pack SHA-256 did not match its catalog")


def _partial_offset(
    partial: Path,
    *,
    descriptor: RuntimePackDescriptor,
    source: RuntimeSource,
) -> int:
    """Return a resumable size without following links outside managed state."""

    try:
        metadata = partial.lstat()
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise RuntimePackDownloadError(
            "PARTIAL_UNAVAILABLE",
            "Runtime Pack partial download could not be inspected",
            retryable=False,
            source=source,
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or (
        stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1
    ):
        try:
            partial.unlink()
        except OSError as exc:
            raise RuntimePackDownloadError(
                "PARTIAL_UNAVAILABLE",
                "Runtime Pack partial download could not be reset safely",
                retryable=False,
                source=source,
            ) from exc
        return 0
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimePackDownloadError(
            "PARTIAL_UNAVAILABLE",
            "Runtime Pack partial download is not a regular file",
            retryable=False,
            source=source,
        )
    if metadata.st_size > descriptor.size_bytes:
        try:
            partial.unlink()
        except OSError as exc:
            raise RuntimePackDownloadError(
                "PARTIAL_UNAVAILABLE",
                "Runtime Pack oversized partial download could not be reset",
                retryable=False,
                source=source,
            ) from exc
        return 0
    return int(metadata.st_size)


def _open_partial_output(
    partial: Path,
    *,
    append: bool,
    expected_offset: int,
    source: RuntimeSource,
):
    """Open a private partial without truncating a link or swapped inode."""

    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
    if append:
        flags |= os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        output_fd = os.open(partial, flags, 0o600)
    except OSError as exc:
        raise RuntimePackDownloadError(
            "PARTIAL_UNAVAILABLE",
            "Runtime Pack partial download could not be opened safely",
            retryable=False,
            source=source,
        ) from exc
    try:
        opened = os.fstat(output_fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise RuntimePackDownloadError(
                "PARTIAL_UNAVAILABLE",
                "Runtime Pack partial download changed type",
                retryable=False,
                source=source,
            )
        if append and opened.st_size != expected_offset:
            raise RuntimePackDownloadError(
                "PARTIAL_CHANGED",
                "Runtime Pack partial download changed while resuming",
                retryable=True,
                source=source,
            )
        if not append:
            os.ftruncate(output_fd, 0)
        try:
            os.fchmod(output_fd, 0o600)
        except (AttributeError, OSError):
            pass
        return os.fdopen(output_fd, "ab" if append else "wb")
    except BaseException:
        os.close(output_fd)
        raise


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if isinstance(status, int):
        return status
    getcode = getattr(response, "getcode", None)
    code = getcode() if callable(getcode) else None
    return int(code) if code is not None else 200


def _parse_content_length(headers: Any) -> int | None:
    value = headers.get("Content-Length") if headers is not None else None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DownloadVerificationError("Runtime Pack response has invalid Content-Length") from exc


def _download_attempt(
    *,
    url: str,
    source: RuntimeSource,
    descriptor: RuntimePackDescriptor,
    partial: Path,
    opener: UrlOpen,
    cancel_event: threading.Event,
    progress_cb: Callable[[int, RuntimeSource], None],
) -> None:
    offset = _partial_offset(partial, descriptor=descriptor, source=source)
    headers = {"User-Agent": "OpenSquilla-runtime-pack/1"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    try:
        response_context = opener(request, timeout=60)
    except urllib.error.HTTPError as exc:
        try:
            if exc.code == 416 and offset == descriptor.size_bytes:
                _verify_archive(partial, descriptor)
                progress_cb(offset, source)
                return
            if exc.code == 416:
                partial.unlink(missing_ok=True)
                raise RuntimePackDownloadError(
                    "INVALID_RANGE",
                    "Runtime Pack partial download was rejected and will restart",
                    retryable=True,
                    source=source,
                ) from exc
        finally:
            exc.close()
        raise RuntimePackDownloadError(
            "HTTP_ERROR",
            f"Runtime Pack source returned HTTP {exc.code}",
            retryable=exc.code == 408 or exc.code == 429 or 500 <= exc.code < 600,
            source=source,
        ) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimePackDownloadError(
            "NETWORK_ERROR",
            "Runtime Pack download could not reach the source",
            retryable=True,
            source=source,
        ) from exc

    with response_context as response:
        final_url = str(getattr(response, "geturl", lambda: url)())
        if not final_url.startswith("https://"):
            raise RuntimePackDownloadError(
                "UNSAFE_REDIRECT",
                "Runtime Pack redirect left HTTPS",
                retryable=False,
                source=source,
            )
        status = _response_status(response)
        headers_obj = getattr(response, "headers", {})
        if status == 206:
            raw_range = headers_obj.get("Content-Range")
            match = _CONTENT_RANGE_RE.fullmatch(str(raw_range or ""))
            if (
                offset <= 0
                or match is None
                or int(match.group(1)) != offset
                or int(match.group(2)) != descriptor.size_bytes - 1
                or int(match.group(3)) != descriptor.size_bytes
            ):
                partial.unlink(missing_ok=True)
                raise RuntimePackDownloadError(
                    "INVALID_RANGE",
                    "Runtime Pack source returned an invalid Content-Range",
                    retryable=False,
                    source=source,
                )
            announced = _parse_content_length(headers_obj)
            if announced is not None and announced != descriptor.size_bytes - offset:
                raise RuntimePackDownloadError(
                    "SIZE_MISMATCH",
                    "Runtime Pack resumed response announced the wrong size",
                    retryable=False,
                    source=source,
                )
            mode = "ab"
            downloaded = offset
        elif status == 200:
            announced = _parse_content_length(headers_obj)
            if announced is not None and announced != descriptor.size_bytes:
                partial.unlink(missing_ok=True)
                raise RuntimePackDownloadError(
                    "SIZE_MISMATCH",
                    "Runtime Pack response announced the wrong size",
                    retryable=False,
                    source=source,
                )
            mode = "wb"
            downloaded = 0
        else:
            raise RuntimePackDownloadError(
                "HTTP_ERROR",
                f"Runtime Pack source returned HTTP {status}",
                retryable=status == 408 or status == 429 or 500 <= status < 600,
                source=source,
            )

        progress_cb(downloaded, source)
        partial.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _open_partial_output(
            partial,
            append=mode == "ab",
            expected_offset=offset,
            source=source,
        ) as output:
            while True:
                if cancel_event.is_set():
                    raise RuntimePackCancelledError("Runtime Pack download was cancelled")
                try:
                    chunk = response.read(_COPY_CHUNK_SIZE)
                except (OSError, http.client.HTTPException) as exc:
                    raise RuntimePackDownloadError(
                        "NETWORK_ERROR",
                        "Runtime Pack download was interrupted",
                        retryable=True,
                        source=source,
                    ) from exc
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > descriptor.size_bytes:
                    partial.unlink(missing_ok=True)
                    raise RuntimePackDownloadError(
                        "SIZE_MISMATCH",
                        "Runtime Pack source exceeded the pinned size",
                        retryable=False,
                        source=source,
                    )
                output.write(chunk)
                progress_cb(downloaded, source)
            output.flush()
            os.fsync(output.fileno())
    if downloaded != descriptor.size_bytes:
        raise RuntimePackDownloadError(
            "INCOMPLETE_DOWNLOAD",
            "Runtime Pack download ended before its pinned size",
            retryable=True,
            source=source,
        )


def _download_with_fallback(
    *,
    catalog: RuntimePackCatalog,
    descriptor: RuntimePackDescriptor,
    partial: Path,
    source_order: tuple[RuntimeSource, ...],
    source_bases: Mapping[RuntimeSource, str],
    opener: UrlOpen,
    cancel_event: threading.Event,
    progress_cb: Callable[[int, RuntimeSource], None],
) -> RuntimeSource:
    last_error: BaseException | None = None
    for source in source_order:
        url = _source_url(source_bases[source], catalog, descriptor)
        for attempt in range(3):
            try:
                _download_attempt(
                    url=url,
                    source=source,
                    descriptor=descriptor,
                    partial=partial,
                    opener=opener,
                    cancel_event=cancel_event,
                    progress_cb=progress_cb,
                )
                _verify_archive(partial, descriptor)
                return source
            except RuntimePackCancelledError:
                raise
            except DownloadVerificationError as exc:
                partial.unlink(missing_ok=True)
                last_error = RuntimePackDownloadError(
                    "VERIFICATION_FAILED",
                    str(exc),
                    retryable=False,
                    source=source,
                )
                break
            except RuntimePackDownloadError as exc:
                last_error = exc
                if not exc.retryable:
                    break
                if attempt < 2 and cancel_event.wait((0.25, 1.0)[attempt]):
                    raise RuntimePackCancelledError("Runtime Pack download was cancelled")
    if isinstance(last_error, RuntimePackDownloadError):
        raise last_error
    raise RuntimePackDownloadError(
        "DOWNLOAD_FAILED",
        "All Runtime Pack sources failed",
        retryable=True,
        source=source_order[-1],
    )


def _load_pack_layout(
    package: Path,
    *,
    catalog: RuntimePackCatalog,
    descriptor: RuntimePackDescriptor,
) -> PackLayout:
    return _load_pack_layout_identity(
        package,
        catalog_version=catalog.catalog_version,
        component_id=descriptor.component_id,
        target=descriptor.target,
        version=descriptor.version,
        activated=False,
    )


def _load_pack_layout_identity(
    package: Path,
    *,
    catalog_version: str,
    component_id: str,
    target: str,
    version: str,
    activated: bool,
) -> PackLayout:
    try:
        with os.scandir(package) as entries:
            root_entries = {entry.name for entry in entries}
    except OSError as exc:
        raise RuntimePackError("Runtime Pack root could not be enumerated") from exc
    expected_root_entries = (
        _ARCHIVE_ROOT_ENTRIES | {_PACK_MARKER}
        if activated
        else _ARCHIVE_ROOT_ENTRIES
    )
    if root_entries != expected_root_entries:
        raise RuntimePackError("Runtime Pack root layout contains unexpected entries")
    manifest_path = package / "pack-manifest.json"
    payload_root = package / "payload"
    licenses_root = package / "licenses"
    sbom_path = package / "SBOM.spdx.json"
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or payload_root.is_symlink()
        or not payload_root.is_dir()
        or licenses_root.is_symlink()
        or not licenses_root.is_dir()
        or sbom_path.is_symlink()
        or not sbom_path.is_file()
    ):
        raise RuntimePackError("Runtime Pack is missing its required root layout")
    raw = _read_json(manifest_path)
    if raw is None or raw.get("schemaVersion") != 1:
        raise RuntimePackError("Runtime Pack manifest schema is invalid")
    expected = {
        "catalogVersion": catalog_version,
        "componentId": component_id,
        "target": target,
        "version": version,
    }
    for field, value in expected.items():
        if raw.get(field) != value:
            raise RuntimePackError(f"Runtime Pack manifest {field} does not match the catalog")
    raw_bins = raw.get("binDirs")
    if not isinstance(raw_bins, list) or not raw_bins:
        raise RuntimePackError("Runtime Pack manifest binDirs must be a non-empty array")
    bin_dirs = tuple(_safe_relative(item, "binDirs", allow_dot=True) for item in raw_bins)
    raw_executables = raw.get("executables")
    if not isinstance(raw_executables, dict) or not raw_executables:
        raise RuntimePackError("Runtime Pack manifest executables must be an object")
    executables = {
        str(name): _safe_relative(path, f"executables.{name}")
        for name, path in raw_executables.items()
        if str(name).strip()
    }
    required_executables = _REQUIRED_EXECUTABLES.get(component_id)
    if (
        required_executables is None
        or set(executables) != set(raw_executables)
        or not required_executables.issubset(executables)
    ):
        raise RuntimePackError("Runtime Pack manifest is missing required executables")
    for relative in bin_dirs:
        directory = _contained_path(payload_root, relative)
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimePackError("Runtime Pack bin directory is missing")
    for relative in executables.values():
        executable = _contained_path(payload_root, relative)
        try:
            resolved_executable = executable.resolve(strict=True)
            resolved_executable.relative_to(payload_root.resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimePackError("Runtime Pack executable escaped its payload") from exc
        if not resolved_executable.is_file():
            raise RuntimePackError("Runtime Pack executable is missing")
        if os.name != "nt" and not os.access(executable, os.X_OK):
            raise RuntimePackError("Runtime Pack executable is not executable")
    return PackLayout(
        component_id=component_id,
        target=target,
        version=version,
        bin_dirs=bin_dirs,
        executables=executables,
    )


def _walk_payload(package: Path) -> list[Path]:
    results: list[Path] = []
    pending = [package]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise RuntimePackError("Runtime Pack payload could not be enumerated") from exc
        for entry in entries:
            path = Path(entry.path)
            if path.name == _PACK_MARKER and path.parent == package:
                continue
            try:
                if entry.is_symlink():
                    results.append(path)
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    results.append(path)
                else:
                    raise RuntimePackError("Runtime Pack contains a special file")
            except OSError as exc:
                raise RuntimePackError("Runtime Pack entry could not be inspected") from exc
    return sorted(results, key=lambda path: path.as_posix())


def _payload_manifest(package: Path) -> tuple[dict[str, dict[str, object]], int]:
    if package.is_symlink() or not package.is_dir():
        raise RuntimePackError("Runtime Pack package must be a real directory")
    manifest: dict[str, dict[str, object]] = {}
    installed_bytes = 0
    package_resolved = package.resolve(strict=True)
    for path in _walk_payload(package):
        relative = path.relative_to(package).as_posix()
        if path.is_symlink():
            try:
                path.resolve(strict=True).relative_to(package_resolved)
            except (OSError, RuntimeError, ValueError) as exc:
                raise RuntimePackError("Runtime Pack symlink escaped its package") from exc
            manifest[relative] = {"type": "symlink", "target": os.readlink(path)}
            continue
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while chunk := source.read(_COPY_CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
        installed_bytes += size
        manifest[relative] = {"type": "file", "size": size, "sha256": digest.hexdigest()}
    return manifest, installed_bytes


def _marker_payload(
    descriptor: RuntimePackDescriptor,
    layout: PackLayout,
    package: Path,
) -> dict[str, object]:
    payload_manifest, installed_bytes = _payload_manifest(package)
    return {
        "schemaVersion": 1,
        "componentId": descriptor.component_id,
        "target": descriptor.target,
        "version": descriptor.version,
        "archiveSha256": descriptor.sha256,
        "binDirs": list(layout.bin_dirs),
        "executables": dict(layout.executables),
        "installedBytes": installed_bytes,
        "payloadManifest": payload_manifest,
    }


def _marker_matches_identity(
    package: Path,
    *,
    component_id: str,
    target: str,
    version: str,
    archive_sha256: str,
) -> bool:
    marker = _read_json(package / _PACK_MARKER)
    if not marker:
        return False
    expected = {
        "schemaVersion": 1,
        "componentId": component_id,
        "target": target,
        "version": version,
        "archiveSha256": archive_sha256,
    }
    if any(marker.get(key) != value for key, value in expected.items()):
        return False
    expected_manifest = marker.get("payloadManifest")
    if not isinstance(expected_manifest, dict):
        return False
    try:
        actual_manifest, installed_bytes = _payload_manifest(package)
        expected_bytes = json.dumps(
            expected_manifest, sort_keys=True, separators=(",", ":")
        ).encode()
        actual_bytes = json.dumps(
            actual_manifest, sort_keys=True, separators=(",", ":")
        ).encode()
    except (OSError, RuntimePackError, TypeError, ValueError):
        return False
    return bool(
        hmac.compare_digest(expected_bytes, actual_bytes)
        and marker.get("installedBytes") == installed_bytes
    )


def _run_probe(descriptor: RuntimePackDescriptor, layout: PackLayout, package: Path) -> None:
    payload = package / "payload"
    executable_paths = {
        name: _contained_path(payload, relative)
        for name, relative in layout.executables.items()
    }
    commands: list[tuple[str, tuple[str, ...], str | None]]
    if descriptor.component_id == "python":
        commands = [("python", ("--version",), descriptor.version.split("+", 1)[0])]
    elif descriptor.component_id == "node":
        commands = [
            ("node", ("--version",), descriptor.version),
            ("npm", ("--version",), None),
            ("npx", ("--version",), None),
        ]
    else:
        commands = [
            ("git", ("--version",), descriptor.version.split(".windows", 1)[0]),
            ("bash", ("--version",), None),
        ]
    environment = dict(os.environ)
    path_key = next((key for key in environment if key.casefold() == "path"), "PATH")
    bins = tuple(_contained_path(payload, value) for value in layout.bin_dirs)
    environment[path_key] = os.pathsep.join(str(path) for path in bins)
    for name, args, expected_version in commands:
        executable = executable_paths[name]
        try:
            completed = subprocess.run(
                [str(executable), *args],
                check=False,
                capture_output=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimePackError(f"Runtime Pack {name} probe could not run") from exc
        output = completed.stdout + completed.stderr
        if completed.returncode != 0 or (
            expected_version is not None
            and expected_version.encode().lower() not in output.lower()
        ):
            raise RuntimePackError(f"Runtime Pack {name} probe failed")


def _package_path(root: Path, descriptor: RuntimePackDescriptor) -> Path:
    return (
        root
        / "packages"
        / descriptor.component_id
        / descriptor.version
        / descriptor.target
    )


def _active_receipt_path(root: Path, component_id: str) -> Path:
    return root / "active" / f"{component_id}.json"


def _queue_pending_cleanup(
    root: Path,
    *,
    package: Path | None = None,
    files: tuple[Path, ...] = (),
) -> None:
    payload: dict[str, object] = {"schemaVersion": 1}
    if package is not None:
        payload["packageRelpath"] = package.relative_to(root).as_posix()
    if files:
        payload["fileRelpaths"] = [path.relative_to(root).as_posix() for path in files]
    if len(payload) == 1:
        return
    _atomic_json(root / "trash" / f"pending-{uuid.uuid4().hex}.json", payload)


def _retire_package_fail_open(root: Path, package: Path) -> None:
    """Move an inactive package out of service and reclaim it when possible."""

    if not _real_path_within(root, package, directory=True):
        return
    trash = root / "trash" / f"package-{uuid.uuid4().hex}"
    try:
        os.replace(package, trash)
    except OSError:
        try:
            _queue_pending_cleanup(root, package=package)
        except (OSError, RuntimePackError, ValueError):
            pass
        return
    shutil.rmtree(trash, ignore_errors=True)


def _component_package_paths(root: Path, component_id: str) -> tuple[Path, ...]:
    component_root = root / "packages" / component_id
    if not _real_path_within(root, component_root, directory=True):
        return ()
    packages: list[Path] = []
    try:
        versions = tuple(component_root.iterdir())
    except OSError:
        return ()
    for version_root in versions:
        if (
            not _real_path_within(root, version_root, directory=True)
            or not _SAFE_IDENTITY_RE.fullmatch(version_root.name)
        ):
            continue
        try:
            targets = tuple(version_root.iterdir())
        except OSError:
            continue
        for package in targets:
            if (
                _real_path_within(root, package, directory=True)
                and _SAFE_IDENTITY_RE.fullmatch(package.name)
            ):
                packages.append(package)
    return tuple(packages)


def _prune_component_packages_fail_open(
    root: Path,
    component_id: str,
    *,
    current: Path,
    previous: Path | None,
) -> None:
    """Keep the active package plus at most one immediately previous package."""

    packages = _component_package_paths(root, component_id)
    keep = {current}
    if previous is not None and previous != current and previous in packages:
        keep.add(previous)
    if len(keep) == 1:
        historical = [package for package in packages if package != current]

        def freshness(path: Path) -> tuple[int, str]:
            try:
                modified = path.stat().st_mtime_ns
            except OSError:
                modified = 0
            return (modified, path.as_posix())

        if historical:
            keep.add(max(historical, key=freshness))
    for package in packages:
        if package not in keep:
            _retire_package_fail_open(root, package)
    component_root = root / "packages" / component_id
    try:
        for version_root in component_root.iterdir():
            if version_root.is_dir() and not version_root.is_symlink():
                try:
                    version_root.rmdir()
                except OSError:
                    pass
    except OSError:
        pass


def _component_download_paths(
    root: Path,
    component_id: str,
    descriptor: RuntimePackDescriptor,
) -> tuple[Path, ...]:
    downloads = root / "downloads"
    paths = {
        downloads / f"{descriptor.sha256}.part",
        downloads / f"{descriptor.sha256}.meta.json",
    }
    try:
        metadata_files = tuple(downloads.iterdir())
    except OSError:
        return tuple(paths)
    for metadata in metadata_files:
        match = re.fullmatch(r"([0-9a-f]{64})\.meta\.json", metadata.name)
        if match is None or metadata.is_symlink() or not metadata.is_file():
            continue
        value = _read_json(metadata)
        if (
            value is not None
            and value.get("componentId") == component_id
            and value.get("sha256") == match.group(1)
        ):
            paths.add(metadata)
            paths.add(downloads / f"{match.group(1)}.part")
    return tuple(sorted(paths, key=lambda path: path.name))


def _remove_component_downloads_fail_open(
    root: Path,
    component_id: str,
    descriptor: RuntimePackDescriptor,
) -> None:
    pending: list[Path] = []
    for path in _component_download_paths(root, component_id, descriptor):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pending.append(path)
    if pending:
        try:
            _queue_pending_cleanup(root, files=tuple(pending))
        except (OSError, RuntimePackError, ValueError):
            pass


def _activation_payload(
    root: Path,
    catalog: RuntimePackCatalog,
    descriptor: RuntimePackDescriptor,
    package: Path,
    layout: PackLayout,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "receiptId": uuid.uuid4().hex,
        "componentId": descriptor.component_id,
        "catalogVersion": catalog.catalog_version,
        "version": descriptor.version,
        "target": descriptor.target,
        "archiveSha256": descriptor.sha256,
        "packageRelpath": package.relative_to(root).as_posix(),
        "binDirs": list(layout.bin_dirs),
        "executables": dict(layout.executables),
        "activatedAtMs": int(time.time() * 1000),
    }


def _write_activation(root: Path, receipt: Mapping[str, Any]) -> None:
    component_id = str(receipt["componentId"])
    receipt_id = str(receipt["receiptId"])
    receipts_root = root / "receipts"
    if not _real_path_within(root, receipts_root, directory=True):
        raise RuntimePackError("Runtime Pack receipt storage is unsafe")
    receipts = receipts_root / component_id
    receipts.mkdir(mode=0o700, exist_ok=True)
    if not _real_path_within(root, receipts, directory=True):
        raise RuntimePackError("Runtime Pack receipt storage is unsafe")
    _atomic_json(receipts / f"{receipt_id}.json", receipt)
    _atomic_json(_active_receipt_path(root, component_id), receipt)


def _active_runtime(
    root: Path,
    component_id: str,
    *,
    required_target: str,
    trusted_archive_sha256: frozenset[str],
) -> ActiveRuntime | None:
    receipt = _read_json(_active_receipt_path(root, component_id))
    if not receipt:
        return None
    catalog_version = str(receipt.get("catalogVersion") or "")
    version = str(receipt.get("version") or "")
    target = str(receipt.get("target") or "")
    archive_sha256 = str(receipt.get("archiveSha256") or "")
    if (
        receipt.get("schemaVersion") != 1
        or receipt.get("componentId") != component_id
        or not _SAFE_IDENTITY_RE.fullmatch(catalog_version)
        or not _SAFE_IDENTITY_RE.fullmatch(version)
        or not _SAFE_IDENTITY_RE.fullmatch(target)
        or target != required_target
        or not _SHA256_RE.fullmatch(archive_sha256)
        or archive_sha256 not in trusted_archive_sha256
    ):
        return None
    try:
        relative = _safe_relative(receipt.get("packageRelpath"), "packageRelpath")
        expected_relative = PurePosixPath(
            "packages", component_id, version, target
        ).as_posix()
        if relative != expected_relative:
            return None
        package = _contained_path(root, relative)
        if not _real_path_within(root, package, directory=True):
            return None
        layout = _load_pack_layout_identity(
            package,
            catalog_version=catalog_version,
            component_id=component_id,
            target=target,
            version=version,
            activated=True,
        )
        if receipt.get("binDirs") != list(layout.bin_dirs) or receipt.get(
            "executables"
        ) != dict(layout.executables):
            return None
        if not _marker_matches_identity(
            package,
            component_id=component_id,
            target=target,
            version=version,
            archive_sha256=archive_sha256,
        ):
            return None
        marker = _read_json(package / _PACK_MARKER) or {}
        installed_bytes = int(marker.get("installedBytes", 0))
    except (OSError, RuntimePackError, TypeError, ValueError):
        return None
    payload = package / "payload"
    return ActiveRuntime(
        component_id=component_id,
        version=version,
        target=target,
        package=package,
        bin_dirs=tuple(_contained_path(payload, value) for value in layout.bin_dirs),
        executables={
            name: _contained_path(payload, value) for name, value in layout.executables.items()
        },
        installed_bytes=installed_bytes,
    )


def _path_sentinel(path: Path) -> tuple[int, int, int, int] | None:
    try:
        value = path.lstat()
    except OSError:
        return None
    return (value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _activation_sentinel(root: Path, component_id: str) -> tuple[object, ...]:
    receipt_path = _active_receipt_path(root, component_id)
    try:
        receipt_bytes = receipt_path.read_bytes()
    except OSError:
        return (_path_sentinel(receipt_path),)
    receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    receipt = _read_json(receipt_path)
    if receipt is None:
        return (_path_sentinel(receipt_path), receipt_digest)
    try:
        relative = _safe_relative(receipt.get("packageRelpath"), "packageRelpath")
        package = _contained_path(root, relative)
        raw_executables = receipt.get("executables")
        if not isinstance(raw_executables, dict):
            raw_executables = {}
        executable_sentinels = tuple(
            (
                str(name),
                _path_sentinel(
                    _contained_path(
                        package / "payload",
                        _safe_relative(path, f"executables.{name}"),
                    )
                ),
            )
            for name, path in sorted(raw_executables.items())
        )
    except (RuntimePackError, TypeError, ValueError):
        return (_path_sentinel(receipt_path), receipt_digest)
    marker_path = package / _PACK_MARKER
    try:
        marker_digest = hashlib.sha256(marker_path.read_bytes()).hexdigest()
    except OSError:
        marker_digest = ""
    return (
        _path_sentinel(receipt_path),
        receipt_digest,
        _path_sentinel(package),
        _path_sentinel(marker_path),
        marker_digest,
        executable_sentinels,
    )


class RuntimePackService:
    """Long-lived manager for one configured state directory and immutable catalog."""

    def __init__(
        self,
        catalog: RuntimePackCatalog | None = None,
        *,
        root: str | Path | None = None,
        configured_state_dir: str | Path | None = None,
        target: str | None = None,
        source_bases: Mapping[RuntimeSource, str] | None = None,
        opener: UrlOpen = urllib.request.urlopen,
    ) -> None:
        self.catalog = catalog or load_default_catalog()
        self.root = Path(root) if root is not None else runtime_packs_root(configured_state_dir)
        self.target = target or runtime_target()
        self._source_bases = dict(source_bases or _default_source_bases())
        self._opener = opener
        self._state_lock = threading.RLock()
        self._download_slots = threading.Semaphore(2)
        self._cancel_events: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._integrity_cache: dict[
            str, tuple[float, tuple[object, ...], ActiveRuntime | None]
        ] = {}
        self._integrity_flights: dict[str, threading.Event] = {}
        self._integrity_lock = threading.Lock()
        _ensure_root(self.root)
        self._recover_and_cleanup_fail_open()

    @property
    def management_supported(self) -> bool:
        return bool(
            self.catalog.finalized
            and self.target in self.catalog.targets
            and _host_is_supported_linux_libc()
        )

    @property
    def source_order(self) -> tuple[RuntimeSource, ...]:
        value = _read_json(self.root / "source-preference.json") or {}
        try:
            last_success = RuntimeSource(str(value.get("lastSuccessfulSource")))
        except ValueError:
            last_success = None
        return _configured_source_order(last_success)

    def _descriptor(self, component_id: str) -> RuntimePackDescriptor | None:
        if component_id not in component_ids():
            raise RuntimePackUnavailableError(f"Unknown Runtime Pack component: {component_id}")
        if not self.management_supported:
            return None
        return self.catalog.descriptor(self.target, component_id)

    def _operation_path(self, component_id: str) -> Path:
        return self.root / "operations" / f"{component_id}.json"

    def _read_operation(self, component_id: str) -> RuntimeOperation | None:
        return RuntimeOperation.from_mapping(_read_json(self._operation_path(component_id)))

    def _write_operation(self, operation: RuntimeOperation) -> RuntimeOperation:
        with self._state_lock:
            _atomic_json(self._operation_path(operation.component_id), operation.to_public_dict())
        return operation

    def _update_operation(
        self,
        operation: RuntimeOperation,
        *,
        state: RuntimeOperationState | None = None,
        progress_bytes: int | None = None,
        source: RuntimeSource | None | object = ...,
        error: RuntimePublicError | None | object = ...,
    ) -> RuntimeOperation:
        next_source = (
            source
            if isinstance(source, RuntimeSource) or source is None
            else operation.source
        )
        next_error = (
            error
            if isinstance(error, RuntimePublicError) or error is None
            else operation.error
        )
        return self._write_operation(
            replace(
                operation,
                state=state if state is not None else operation.state,
                progress_bytes=(
                    max(0, progress_bytes)
                    if progress_bytes is not None
                    else operation.progress_bytes
                ),
                source=next_source,
                updated_at_ms=int(time.time() * 1000),
                error=next_error,
            )
        )

    def _new_operation(
        self,
        component_id: str,
        kind: RuntimeOperationKind,
        *,
        total_bytes: int,
    ) -> RuntimeOperation:
        now = int(time.time() * 1000)
        return RuntimeOperation(
            operation_id=uuid.uuid4().hex,
            component_id=component_id,
            kind=kind,
            state=RuntimeOperationState.QUEUED,
            progress_bytes=0,
            total_bytes=max(0, total_bytes),
            source=None,
            started_at_ms=now,
            updated_at_ms=now,
        )

    def _operation_claim_is_current(self, operation: RuntimeOperation) -> bool:
        """Return whether this worker still owns the shared component operation."""

        current = self._read_operation(operation.component_id)
        return bool(
            current is not None
            and current.operation_id == operation.operation_id
            and current.kind is operation.kind
            and current.state in _ACTIVE_STATES
        )

    def _operation_after_claim_contention(
        self,
        component_id: str,
        previous_operation_id: str | None,
    ) -> RuntimeOperation:
        """Join the operation that won a cross-process claim race."""

        deadline = time.monotonic() + 1.0
        while True:
            with self._state_lock:
                current = self._read_operation(component_id)
            if current is not None and current.operation_id != previous_operation_id:
                return current
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        raise RuntimePackError(
            "Runtime Pack component state is busy; refresh its status and retry"
        )

    def _recover_interrupted_operations(self) -> None:
        for component_id in component_ids():
            operation = self._read_operation(component_id)
            if operation is None or operation.state not in _ACTIVE_STATES:
                continue
            self._update_operation(
                operation,
                state=RuntimeOperationState.INTERRUPTED,
                error=RuntimePublicError(
                    code="INTERRUPTED",
                    message="Runtime Pack operation was interrupted; it can be resumed.",
                    retryable=True,
                    source=operation.source,
                ),
            )

    def _recover_and_cleanup_fail_open(self) -> None:
        """Recover only while no other process can own managed component state."""

        try:
            with ExitStack() as locks:
                for component_id in component_ids():
                    locks.enter_context(
                        ManagedArtifactInstallLock(self.root, component_id, 0.0)
                    )
                self._recover_interrupted_operations()
                self._cleanup_staging_fail_open()
                self._cleanup_trash_fail_open()
        except Exception:
            # Another Gateway/CLI may be actively installing or removing a
            # component. Its staging, operation, and rollback directories are
            # live state and must not be reclassified or reclaimed here.
            _LOG.debug(
                "Runtime Pack recovery deferred while managed state is busy",
                exc_info=True,
            )

    def _cleanup_trash_fail_open(self) -> None:
        try:
            for candidate in (self.root / "trash").iterdir():
                if candidate.is_dir() and not candidate.is_symlink():
                    shutil.rmtree(candidate, ignore_errors=True)
                elif candidate.name.endswith(".json"):
                    pending = _read_json(candidate) or {}
                    complete = True
                    relative = pending.get("packageRelpath")
                    if isinstance(relative, str):
                        try:
                            safe_relative = _safe_relative(relative, "packageRelpath")
                            if PurePosixPath(safe_relative).parts[:1] != ("packages",):
                                raise RuntimePackError("pending package is outside packages")
                            package = _contained_path(self.root, safe_relative)
                            if not _real_path_within(
                                self.root,
                                package,
                                directory=True,
                            ):
                                if package.exists() or package.is_symlink():
                                    raise RuntimePackError(
                                        "pending package must be a real managed directory"
                                    )
                                raise FileNotFoundError(package)
                            shutil.rmtree(package)
                        except FileNotFoundError:
                            pass
                        except (OSError, RuntimePackError):
                            complete = False
                    raw_files = pending.get("fileRelpaths", [])
                    if not isinstance(raw_files, list):
                        complete = False
                        raw_files = []
                    for raw_file in raw_files:
                        try:
                            safe_relative = _safe_relative(raw_file, "fileRelpaths")
                            relative_path = PurePosixPath(safe_relative)
                            if (
                                relative_path.parts[:1] != ("downloads",)
                                or len(relative_path.parts) != 2
                                or re.fullmatch(
                                    r"[0-9a-f]{64}\.(?:part|meta\.json)",
                                    relative_path.name,
                                )
                                is None
                            ):
                                raise RuntimePackError("pending file is outside downloads")
                            _contained_path(self.root, safe_relative).unlink(missing_ok=True)
                        except (OSError, RuntimePackError, TypeError, ValueError):
                            complete = False
                    if complete:
                        candidate.unlink(missing_ok=True)
        except OSError:
            return

    def _cleanup_staging_fail_open(self) -> None:
        try:
            candidates = tuple((self.root / "staging").iterdir())
        except OSError:
            return
        for candidate in candidates:
            try:
                if candidate.is_dir() and not candidate.is_symlink():
                    shutil.rmtree(candidate, ignore_errors=True)
                elif candidate.is_file() and not candidate.is_symlink():
                    candidate.unlink(missing_ok=True)
            except OSError:
                continue

    def invalidate_integrity_cache(self, component_id: str | None = None) -> None:
        """Invalidate cached payload verification after activation or removal."""

        with self._integrity_lock:
            if component_id is None:
                self._integrity_cache.clear()
                return
            self._integrity_cache.pop(component_id, None)

    def _failed_unsupported_operation(
        self,
        component_id: str,
        kind: RuntimeOperationKind,
    ) -> RuntimeOperation:
        operation = self._new_operation(component_id, kind, total_bytes=0)
        return self._update_operation(
            operation,
            state=RuntimeOperationState.FAILED,
            error=RuntimePublicError(
                code="UNSUPPORTED",
                message="Runtime Pack management is unavailable for this platform or build.",
                retryable=False,
            ),
        )

    def start_install(self, component_id: str) -> RuntimeOperation:
        descriptor = self._descriptor(component_id)
        if descriptor is None:
            return self._failed_unsupported_operation(
                component_id, RuntimeOperationKind.INSTALL
            )
        with self._state_lock:
            existing = self._read_operation(component_id)
            if existing is not None and existing.state in _ACTIVE_STATES:
                return existing
            previous_operation_id = existing.operation_id if existing is not None else None
        try:
            # The fast read above avoids waiting on a long-running worker.  The
            # zero-timeout lock closes the read/write race between processes;
            # the winner publishes its operation before releasing this claim.
            with ManagedArtifactInstallLock(self.root, component_id, 0.0):
                with self._state_lock:
                    existing = self._read_operation(component_id)
                    if existing is not None and existing.state in _ACTIVE_STATES:
                        return existing
                    operation = self._write_operation(
                        self._new_operation(
                            component_id,
                            RuntimeOperationKind.INSTALL,
                            total_bytes=descriptor.size_bytes,
                        )
                    )
                    cancel_event = threading.Event()
                    self._cancel_events[operation.operation_id] = cancel_event
                    thread = threading.Thread(
                        target=self._install_worker,
                        args=(operation, descriptor, cancel_event),
                        name=f"runtime-pack-{component_id}",
                        daemon=True,
                    )
                    self._threads[operation.operation_id] = thread
                    try:
                        thread.start()
                    except (OSError, RuntimeError):
                        self._cancel_events.pop(operation.operation_id, None)
                        self._threads.pop(operation.operation_id, None)
                        operation = self._update_operation(
                            operation,
                            state=RuntimeOperationState.FAILED,
                            error=RuntimePublicError(
                                code="INSTALL_FAILED",
                                message="Runtime Pack installation could not be started.",
                                retryable=True,
                            ),
                        )
                    return operation
        except ManagedArtifactError:
            return self._operation_after_claim_contention(
                component_id,
                previous_operation_id,
            )

    def cancel(self, component_id: str, operation_id: str) -> RuntimeOperation:
        if component_id not in component_ids():
            raise RuntimePackUnavailableError(f"Unknown Runtime Pack component: {component_id}")
        with self._state_lock:
            operation = self._read_operation(component_id)
            if operation is None or operation.operation_id != operation_id:
                raise RuntimePackError("Runtime Pack operation does not exist")
            if operation.state.terminal:
                return operation
            if operation.kind is not RuntimeOperationKind.INSTALL:
                raise RuntimePackError("Runtime Pack removal cannot be cancelled")
            if operation.state is RuntimeOperationState.ACTIVATING:
                # Activation is an atomic commit boundary. Once entered, do not
                # report a cancellation that can no longer be honored safely.
                return operation
            event = self._cancel_events.get(operation_id)
            if event is not None:
                event.set()
            return self._update_operation(
                operation,
                state=RuntimeOperationState.CANCELLING,
            )

    def remove(self, component_id: str) -> RuntimeOperation:
        descriptor = self._descriptor(component_id)
        if descriptor is None:
            return self._failed_unsupported_operation(
                component_id, RuntimeOperationKind.REMOVE
            )
        with self._state_lock:
            existing = self._read_operation(component_id)
            if existing is not None and existing.state in _ACTIVE_STATES:
                return existing
            previous_operation_id = existing.operation_id if existing is not None else None
        try:
            with ManagedArtifactInstallLock(self.root, component_id, 0.0):
                with self._state_lock:
                    existing = self._read_operation(component_id)
                    if existing is not None and existing.state in _ACTIVE_STATES:
                        return existing
                    operation = self._write_operation(
                        self._new_operation(
                            component_id,
                            RuntimeOperationKind.REMOVE,
                            total_bytes=0,
                        )
                    )
                    thread = threading.Thread(
                        target=self._remove_worker,
                        args=(operation, descriptor),
                        name=f"runtime-pack-remove-{component_id}",
                        daemon=True,
                    )
                    self._threads[operation.operation_id] = thread
                    try:
                        thread.start()
                    except (OSError, RuntimeError):
                        self._threads.pop(operation.operation_id, None)
                        operation = self._update_operation(
                            operation,
                            state=RuntimeOperationState.FAILED,
                            error=RuntimePublicError(
                                code="REMOVE_FAILED",
                                message="Runtime Pack removal could not be started.",
                                retryable=True,
                            ),
                        )
                    return operation
        except ManagedArtifactError:
            return self._operation_after_claim_contention(
                component_id,
                previous_operation_id,
            )

    def _progress_callback(
        self,
        operation: RuntimeOperation,
    ) -> tuple[Callable[[int, RuntimeSource], None], list[RuntimeOperation]]:
        holder = [operation]
        last_write = [0.0]

        def callback(downloaded: int, source: RuntimeSource) -> None:
            now = time.monotonic()
            if now - last_write[0] < 0.2 and downloaded < operation.total_bytes:
                return
            holder[0] = self._update_operation(
                holder[0],
                state=RuntimeOperationState.DOWNLOADING,
                progress_bytes=downloaded,
                source=source,
                error=None,
            )
            last_write[0] = now

        return callback, holder

    def _install_worker(
        self,
        operation: RuntimeOperation,
        descriptor: RuntimePackDescriptor,
        cancel_event: threading.Event,
    ) -> None:
        try:
            with self._download_slots, ManagedArtifactInstallLock(
                self.root, descriptor.component_id, _INSTALL_LOCK_TIMEOUT_SECONDS
            ):
                if not self._operation_claim_is_current(operation):
                    return
                operation = self._install_locked(operation, descriptor, cancel_event)
        except RuntimePackCancelledError:
            operation = self._update_operation(
                self._read_operation(operation.component_id) or operation,
                state=RuntimeOperationState.CANCELLED,
                error=None,
            )
        except RuntimePackDownloadError as exc:
            operation = self._update_operation(
                self._read_operation(operation.component_id) or operation,
                state=RuntimeOperationState.FAILED,
                error=RuntimePublicError(
                    code=exc.code,
                    message=str(exc),
                    retryable=exc.retryable,
                    source=exc.source,
                ),
            )
        except Exception:
            _LOG.exception(
                "Runtime Pack installation failed for %s",
                operation.component_id,
            )
            operation = self._update_operation(
                self._read_operation(operation.component_id) or operation,
                state=RuntimeOperationState.FAILED,
                error=RuntimePublicError(
                    code="INSTALL_FAILED",
                    message=(
                        "Runtime Pack installation failed. Check disk space and "
                        "permissions, then retry."
                    ),
                    retryable=True,
                    source=operation.source,
                ),
            )
        finally:
            with self._state_lock:
                self._cancel_events.pop(operation.operation_id, None)
                self._threads.pop(operation.operation_id, None)

    def _install_locked(
        self,
        operation: RuntimeOperation,
        descriptor: RuntimePackDescriptor,
        cancel_event: threading.Event,
    ) -> RuntimeOperation:
        _ensure_root(self.root)
        if cancel_event.is_set():
            raise RuntimePackCancelledError("Runtime Pack installation was cancelled")
        required_space = (
            descriptor.size_bytes
            + descriptor.unpacked_size_bytes
            + max(_DISK_SAFETY_BYTES, descriptor.unpacked_size_bytes // 10)
        )
        if shutil.disk_usage(self.root).free < required_space:
            raise RuntimePackError("Not enough disk space to install this Runtime Pack")
        partial = self.root / "downloads" / f"{descriptor.sha256}.part"
        meta = self.root / "downloads" / f"{descriptor.sha256}.meta.json"
        _atomic_json(
            meta,
            {
                "schemaVersion": 1,
                "catalogVersion": self.catalog.catalog_version,
                "componentId": descriptor.component_id,
                "target": descriptor.target,
                "sizeBytes": descriptor.size_bytes,
                "sha256": descriptor.sha256,
            },
        )
        callback, holder = self._progress_callback(operation)
        source: RuntimeSource
        try:
            _verify_archive(partial, descriptor)
            source = self.source_order[0]
            callback(descriptor.size_bytes, source)
        except DownloadVerificationError:
            if partial.is_file() and partial.stat().st_size == descriptor.size_bytes:
                partial.unlink(missing_ok=True)
            source = _download_with_fallback(
                catalog=self.catalog,
                descriptor=descriptor,
                partial=partial,
                source_order=self.source_order,
                source_bases=self._source_bases,
                opener=self._opener,
                cancel_event=cancel_event,
                progress_cb=callback,
            )
        operation = holder[0]
        if cancel_event.is_set():
            raise RuntimePackCancelledError("Runtime Pack installation was cancelled")
        operation = self._update_operation(
            operation,
            state=RuntimeOperationState.VERIFYING,
            progress_bytes=descriptor.size_bytes,
            source=source,
        )
        _verify_archive(partial, descriptor)
        if cancel_event.is_set():
            raise RuntimePackCancelledError("Runtime Pack installation was cancelled")
        staging = Path(
            tempfile.mkdtemp(prefix=f"{descriptor.component_id}-", dir=self.root / "staging")
        )
        quarantine: Path | None = None
        package = _package_path(self.root, descriptor)
        try:
            if cancel_event.is_set():
                raise RuntimePackCancelledError("Runtime Pack installation was cancelled")
            operation = self._update_operation(
                operation, state=RuntimeOperationState.EXTRACTING
            )
            extracted = staging / "package"
            try:
                extract_managed_archive(
                    partial,
                    extracted,
                    archive_type=descriptor.archive_type,
                    compressed_size=descriptor.size_bytes,
                    max_extracted_bytes=descriptor.unpacked_size_bytes,
                )
            except UnsafeArchiveError:
                partial.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                raise
            if cancel_event.is_set():
                raise RuntimePackCancelledError("Runtime Pack installation was cancelled")
            try:
                layout = _load_pack_layout(
                    extracted, catalog=self.catalog, descriptor=descriptor
                )
            except RuntimePackError:
                partial.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                raise
            unpacked = sum(
                path.stat().st_size
                for path in _walk_payload(extracted)
                if path.is_file() and not path.is_symlink()
            )
            if unpacked > descriptor.unpacked_size_bytes:
                partial.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                raise RuntimePackError("Runtime Pack exceeded its pinned unpacked size")
            if cancel_event.is_set():
                raise RuntimePackCancelledError("Runtime Pack installation was cancelled")
            operation = self._update_operation(
                operation, state=RuntimeOperationState.PROBING
            )
            _run_probe(descriptor, layout, extracted)
            if cancel_event.is_set():
                raise RuntimePackCancelledError("Runtime Pack installation was cancelled")
            _atomic_json(extracted / _PACK_MARKER, _marker_payload(descriptor, layout, extracted))
            previous_active = self.active_runtime(descriptor.component_id)
            previous_package = (
                previous_active.package if previous_active is not None else None
            )
            operation = self._update_operation(
                operation, state=RuntimeOperationState.ACTIVATING
            )
            if cancel_event.is_set():
                raise RuntimePackCancelledError("Runtime Pack installation was cancelled")
            if _ensure_package_parent(self.root, descriptor) != package:
                raise RuntimePackError("Runtime Pack package target changed unexpectedly")
            if package.exists():
                quarantine = self.root / "trash" / f"{descriptor.component_id}-{uuid.uuid4().hex}"
                os.replace(package, quarantine)
            failed_package: Path | None = None
            try:
                os.replace(extracted, package)
                receipt = _activation_payload(
                    self.root, self.catalog, descriptor, package, layout
                )
                _write_activation(self.root, receipt)
            except BaseException:
                try:
                    if package.exists():
                        failed_package = (
                            self.root
                            / "trash"
                            / f"failed-{descriptor.component_id}-{uuid.uuid4().hex}"
                        )
                        os.replace(package, failed_package)
                    if quarantine is not None:
                        os.replace(quarantine, package)
                        quarantine = None
                except BaseException as rollback_exc:
                    raise RuntimePackError(
                        "Runtime Pack activation failed and could not restore its previous package"
                    ) from rollback_exc
                finally:
                    if failed_package is not None:
                        shutil.rmtree(failed_package, ignore_errors=True)
                    self.invalidate_integrity_cache(descriptor.component_id)
                raise
            self.invalidate_integrity_cache(descriptor.component_id)
            if quarantine is not None:
                shutil.rmtree(quarantine, ignore_errors=True)
            _prune_component_packages_fail_open(
                self.root,
                descriptor.component_id,
                current=package,
                previous=previous_package,
            )
            partial.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            _atomic_json(
                self.root / "source-preference.json",
                {"lastSuccessfulSource": source.value},
            )
            return self._update_operation(
                operation,
                state=RuntimeOperationState.COMPLETED,
                progress_bytes=descriptor.size_bytes,
                source=source,
                error=None,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _remove_worker(
        self,
        operation: RuntimeOperation,
        descriptor: RuntimePackDescriptor,
    ) -> None:
        try:
            with ManagedArtifactInstallLock(
                self.root, descriptor.component_id, _INSTALL_LOCK_TIMEOUT_SECONDS
            ):
                if not self._operation_claim_is_current(operation):
                    return
                _ensure_root(self.root)
                operation = self._update_operation(
                    operation, state=RuntimeOperationState.REMOVING
                )
                active_path = _active_receipt_path(self.root, descriptor.component_id)
                active_path.unlink(missing_ok=True)
                self.invalidate_integrity_cache(descriptor.component_id)
                _remove_component_downloads_fail_open(
                    self.root,
                    descriptor.component_id,
                    descriptor,
                )
                component_packages = self.root / "packages" / descriptor.component_id
                if component_packages.exists():
                    _retire_package_fail_open(self.root, component_packages)
                operation = self._update_operation(
                    operation,
                    state=RuntimeOperationState.COMPLETED,
                    error=None,
                )
        except Exception:
            _LOG.exception(
                "Runtime Pack removal failed for %s",
                operation.component_id,
            )
            self._update_operation(
                self._read_operation(operation.component_id) or operation,
                state=RuntimeOperationState.FAILED,
                error=RuntimePublicError(
                    code="REMOVE_FAILED",
                    message="Runtime Pack removal failed. Retry after closing running tools.",
                    retryable=True,
                ),
            )
        finally:
            with self._state_lock:
                self._threads.pop(operation.operation_id, None)

    def active_runtime(self, component_id: str) -> ActiveRuntime | None:
        descriptor = self._descriptor(component_id)
        if descriptor is None:
            return None
        trusted_archive_sha256 = frozenset(
            (descriptor.sha256, *descriptor.trusted_archive_sha256)
        )
        sentinel = _activation_sentinel(self.root, component_id)
        while True:
            with self._integrity_lock:
                cached = self._integrity_cache.get(component_id)
                if (
                    cached is not None
                    and time.monotonic() - cached[0] < _INTEGRITY_CACHE_TTL_SECONDS
                    and hmac.compare_digest(repr(cached[1]), repr(sentinel))
                ):
                    return cached[2]
                flight = self._integrity_flights.get(component_id)
                if flight is None:
                    flight = threading.Event()
                    self._integrity_flights[component_id] = flight
                    leader = True
                else:
                    leader = False
            if leader:
                break
            flight.wait(_PROBE_TIMEOUT_SECONDS)
            sentinel = _activation_sentinel(self.root, component_id)

        result: ActiveRuntime | None = None
        try:
            result = _active_runtime(
                self.root,
                component_id,
                required_target=self.target,
                trusted_archive_sha256=trusted_archive_sha256,
            )
            final_sentinel = _activation_sentinel(self.root, component_id)
            if hmac.compare_digest(repr(sentinel), repr(final_sentinel)):
                with self._integrity_lock:
                    self._integrity_cache[component_id] = (
                        time.monotonic(),
                        final_sentinel,
                        result,
                    )
            return result
        finally:
            with self._integrity_lock:
                completed = self._integrity_flights.pop(component_id, None)
                if completed is not None:
                    completed.set()

    def status(self) -> RuntimePackStatus:
        statuses: list[RuntimeComponentStatus] = []
        active_operation = False
        for component_id in component_ids():
            descriptor = self._descriptor(component_id)
            operation = self._read_operation(component_id)
            active_operation = active_operation or bool(
                operation is not None and operation.state in _ACTIVE_STATES
            )
            partial_size = 0
            if descriptor is not None:
                partial = self.root / "downloads" / f"{descriptor.sha256}.part"
                try:
                    partial_size = min(partial.stat().st_size, descriptor.size_bytes)
                except OSError:
                    partial_size = 0
            active = self.active_runtime(component_id) if descriptor is not None else None
            receipt_exists = _active_receipt_path(self.root, component_id).is_file()
            if descriptor is None:
                availability = RuntimeAvailability.UNSUPPORTED
            elif active is not None:
                availability = RuntimeAvailability.READY
            elif receipt_exists:
                availability = RuntimeAvailability.CORRUPT
            else:
                availability = RuntimeAvailability.MISSING
            error = operation.error if operation is not None else None
            statuses.append(
                RuntimeComponentStatus(
                    component_id=component_id,
                    availability=availability,
                    catalog_version=(
                        self.catalog.catalog_version if descriptor is not None else None
                    ),
                    active_version=active.version if active is not None else None,
                    installed_bytes=(
                        active.installed_bytes if active is not None else None
                    ),
                    removable=receipt_exists or active is not None,
                    resume_available=bool(
                        descriptor is not None and 0 < partial_size < descriptor.size_bytes
                    ),
                    resume_bytes=partial_size,
                    operation=operation,
                    last_error=error,
                )
            )
        return RuntimePackStatus(
            schema_version=1,
            management_supported=self.management_supported,
            target=self.target,
            catalog_version=self.catalog.catalog_version,
            source_order=self.source_order,
            components=tuple(statuses),
            next_poll_after_ms=750 if active_operation else 5_000,
        )

    def wait_for_operation(
        self,
        operation_id: str,
        *,
        timeout: float = 30.0,
    ) -> RuntimeOperation | None:
        """Test/CLI helper; RPC callers should poll :meth:`status`."""

        thread = self._threads.get(operation_id)
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
        for component_id in component_ids():
            operation = self._read_operation(component_id)
            if operation is not None and operation.operation_id == operation_id:
                return operation
        return None
