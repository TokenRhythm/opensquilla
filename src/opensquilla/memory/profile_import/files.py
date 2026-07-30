"""Fixed target resolution, safe reads, quotas, and unified diffs."""

from __future__ import annotations

import ctypes
import difflib
import hashlib
import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from opensquilla.memory.profile_import.errors import (
    ProfileImportInvalidOutputError,
    ProfileImportWriteError,
)
from opensquilla.memory.profile_import.models import (
    DecisionTarget,
    FileChangeStatus,
    FusionOutput,
    InternalFilePlan,
    ProfileImportFileDiff,
    ProfileImportPaths,
    ProfileImportQuotas,
    ProfileImportReceipt,
)
from opensquilla.profile_import_io import (
    BoundProfileReadError,
    capture_bound_profile_directory,
    capture_bound_profile_file,
    native_io_path,
)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SDDL_REVISION_1 = 1
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_ALREADY_EXISTS = 183
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_SE_FILE_OBJECT = 1
_FILE_ATTRIBUTE_TAG_INFO = 9
_FILE_ID_INFO = 18


class _WindowsFileAttributeTagInfo(ctypes.Structure):
    _fields_ = [
        ("file_attributes", ctypes.c_uint32),
        ("reparse_tag", ctypes.c_uint32),
    ]


class _WindowsFileId128(ctypes.Structure):
    _fields_ = [("identifier", ctypes.c_ubyte * 16)]


class _WindowsFileIdInfo(ctypes.Structure):
    _fields_ = [
        ("volume_serial_number", ctypes.c_uint64),
        ("file_id", _WindowsFileId128),
    ]


class _WindowsSidAndAttributes(ctypes.Structure):
    _fields_ = [
        ("sid", ctypes.c_void_p),
        ("attributes", ctypes.c_uint32),
    ]


class _WindowsTokenUser(ctypes.Structure):
    _fields_ = [("user", _WindowsSidAndAttributes)]


class _WindowsSecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("security_descriptor", ctypes.c_void_p),
        ("inherit_handle", ctypes.c_int),
    ]


def _is_link_or_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    return stat.S_ISLNK(value.st_mode) or bool(
        attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _private_windows_sddl(user_sid: str, *, directory: bool) -> str:
    inheritance = "OICI" if directory else ""
    return (
        f"D:P(A;{inheritance};FA;;;{user_sid})"
        f"(A;{inheritance};FA;;;SY)"
    )


def _windows_last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if callable(getter) else 0


class _CtypesWindowsPrivateAcl:
    """Thin handle-bound Win32 ACL adapter; injectable in platform-neutral tests."""

    def __init__(self) -> None:
        loader = getattr(ctypes, "WinDLL")
        self.advapi32 = loader("advapi32", use_last_error=True)
        self.kernel32 = loader("kernel32", use_last_error=True)

    def current_user_sid(self) -> str:
        open_token = self.advapi32.OpenProcessToken
        get_token = self.advapi32.GetTokenInformation
        convert_sid = self.advapi32.ConvertSidToStringSidW
        get_process = self.kernel32.GetCurrentProcess
        close_handle = self.kernel32.CloseHandle
        local_free = self.kernel32.LocalFree
        open_token.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        open_token.restype = ctypes.c_int
        get_token.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        get_token.restype = ctypes.c_int
        convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        convert_sid.restype = ctypes.c_int
        get_process.argtypes = []
        get_process.restype = ctypes.c_void_p
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p

        token = ctypes.c_void_p()
        if not open_token(get_process(), _TOKEN_QUERY, ctypes.byref(token)):
            error_number = _windows_last_error()
            raise OSError(error_number, "cannot open the current Windows process token")
        try:
            required = ctypes.c_uint32()
            first = get_token(
                token,
                _TOKEN_USER,
                None,
                0,
                ctypes.byref(required),
            )
            error_number = _windows_last_error()
            if first or required.value == 0 or error_number != _ERROR_INSUFFICIENT_BUFFER:
                raise OSError(
                    error_number,
                    "cannot size the current Windows token user",
                )
            buffer = ctypes.create_string_buffer(required.value)
            if not get_token(
                token,
                _TOKEN_USER,
                buffer,
                required.value,
                ctypes.byref(required),
            ):
                error_number = _windows_last_error()
                raise OSError(error_number, "cannot read the current Windows token user")
            token_user = ctypes.cast(
                buffer,
                ctypes.POINTER(_WindowsTokenUser),
            ).contents
            string_sid = ctypes.c_void_p()
            if not convert_sid(token_user.user.sid, ctypes.byref(string_sid)):
                error_number = _windows_last_error()
                raise OSError(error_number, "cannot format the current Windows user SID")
            try:
                return ctypes.wstring_at(string_sid)
            finally:
                local_free(string_sid)
        finally:
            close_handle(token)

    @contextmanager
    def open_bound(
        self,
        path: Path,
        *,
        directory: bool,
        expected_device: int,
        expected_inode: int,
    ) -> Iterator[object]:
        create_file = self.kernel32.CreateFileW
        get_information = self.kernel32.GetFileInformationByHandleEx
        close_handle = self.kernel32.CloseHandle
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        get_information.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        get_information.restype = ctypes.c_int
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int

        flags = _FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= _FILE_FLAG_BACKUP_SEMANTICS
        handle = create_file(
            native_io_path(path),
            _READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            flags,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle in {None, invalid_handle}:
            error_number = _windows_last_error()
            raise OSError(error_number, f"cannot bind private Windows path: {path}")
        try:
            attributes = _WindowsFileAttributeTagInfo()
            if not get_information(
                handle,
                _FILE_ATTRIBUTE_TAG_INFO,
                ctypes.byref(attributes),
                ctypes.sizeof(attributes),
            ):
                error_number = _windows_last_error()
                raise OSError(
                    error_number,
                    f"cannot inspect bound private Windows path: {path}",
                )
            file_id = _WindowsFileIdInfo()
            if not get_information(
                handle,
                _FILE_ID_INFO,
                ctypes.byref(file_id),
                ctypes.sizeof(file_id),
            ):
                error_number = _windows_last_error()
                raise OSError(
                    error_number,
                    f"cannot inspect bound private Windows path identity: {path}",
                )
            file_attributes = int(attributes.file_attributes)
            is_directory = bool(file_attributes & _FILE_ATTRIBUTE_DIRECTORY)
            identity = (
                int(file_id.volume_serial_number),
                int.from_bytes(bytes(file_id.file_id.identifier), "little"),
            )
            if (
                file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                or is_directory != directory
                or (
                    expected_inode
                    and identity != (expected_device, expected_inode)
                )
            ):
                raise OSError(
                    0,
                    f"private Windows path changed or is a reparse point: {path}",
                )
            yield handle
        finally:
            close_handle(handle)

    @contextmanager
    def _security_descriptor(self, sddl: str) -> Iterator[tuple[object, object]]:
        convert = self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        get_dacl = self.advapi32.GetSecurityDescriptorDacl
        local_free = self.kernel32.LocalFree
        convert.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        convert.restype = ctypes.c_int
        get_dacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_int),
        ]
        get_dacl.restype = ctypes.c_int
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p

        descriptor = ctypes.c_void_p()
        descriptor_size = ctypes.c_uint32()
        if not convert(
            sddl,
            _SDDL_REVISION_1,
            ctypes.byref(descriptor),
            ctypes.byref(descriptor_size),
        ):
            error_number = _windows_last_error()
            raise OSError(
                error_number,
                "cannot create a private Windows security descriptor",
            )
        try:
            present = ctypes.c_int()
            defaulted = ctypes.c_int()
            dacl = ctypes.c_void_p()
            if (
                not get_dacl(
                    descriptor,
                    ctypes.byref(present),
                    ctypes.byref(dacl),
                    ctypes.byref(defaulted),
                )
                or not present.value
                or not dacl.value
            ):
                error_number = _windows_last_error()
                raise OSError(error_number, "private Windows DACL is missing")
            yield descriptor, dacl
        finally:
            local_free(descriptor)

    def set_protected_dacl(self, handle: object, sddl: str) -> None:
        set_security = self.advapi32.SetSecurityInfo
        set_security.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        set_security.restype = ctypes.c_uint32
        with self._security_descriptor(sddl) as (_descriptor, dacl):
            result = set_security(
                handle,
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl,
                None,
            )
            if result:
                raise OSError(int(result), "cannot restrict bound private Windows path")

    def create_directory(self, path: Path, sddl: str) -> None:
        create_directory = self.kernel32.CreateDirectoryW
        create_directory.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(_WindowsSecurityAttributes),
        ]
        create_directory.restype = ctypes.c_int
        with self._security_descriptor(sddl) as (descriptor, _dacl):
            attributes = _WindowsSecurityAttributes(
                length=ctypes.sizeof(_WindowsSecurityAttributes),
                security_descriptor=descriptor,
                inherit_handle=0,
            )
            if create_directory(native_io_path(path), ctypes.byref(attributes)):
                return
            error_number = _windows_last_error()
            if error_number == _ERROR_ALREADY_EXISTS:
                raise FileExistsError(error_number, "directory already exists", str(path))
            raise OSError(
                error_number,
                f"cannot create private Windows directory: {path}",
            )


def _apply_windows_private_dacl(
    path: Path,
    *,
    directory: bool,
    expected_device: int,
    expected_inode: int,
    native: Any | None = None,
) -> None:
    """Install a protected current-user-and-SYSTEM DACL through a bound handle."""

    api = native or _CtypesWindowsPrivateAcl()
    sddl = _private_windows_sddl(api.current_user_sid(), directory=directory)
    with api.open_bound(
        path,
        directory=directory,
        expected_device=expected_device,
        expected_inode=expected_inode,
    ) as handle:
        api.set_protected_dacl(handle, sddl)


def _create_windows_private_directory(
    path: Path,
    *,
    native: Any | None = None,
) -> None:
    api = native or _CtypesWindowsPrivateAcl()
    sddl = _private_windows_sddl(api.current_user_sid(), directory=True)
    api.create_directory(path, sddl)


def restrict_private_path(path: Path, *, directory: bool) -> None:
    """Restrict an owned private path without following a link or reparse point."""

    try:
        before = os.lstat(native_io_path(path))
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if _is_link_or_reparse(before) or not expected_type(before.st_mode):
            raise ProfileImportWriteError(
                f"private profile import path has an unsafe type: {path}"
            )
        if os.name == "nt":
            _apply_windows_private_dacl(
                path,
                directory=directory,
                expected_device=int(before.st_dev),
                expected_inode=int(before.st_ino),
            )
            after = os.lstat(native_io_path(path))
            if (
                _is_link_or_reparse(after)
                or not expected_type(after.st_mode)
                or (int(after.st_dev), int(after.st_ino))
                != (int(before.st_dev), int(before.st_ino))
            ):
                raise ProfileImportWriteError(
                    f"private profile import path changed during permission hardening: {path}"
                )
            return

        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(native_io_path(path), flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not expected_type(opened.st_mode)
                or (int(opened.st_dev), int(opened.st_ino))
                != (int(before.st_dev), int(before.st_ino))
            ):
                raise ProfileImportWriteError(
                    f"private profile import path changed during permission hardening: {path}"
                )
            os.fchmod(descriptor, 0o700 if directory else 0o600)
        finally:
            os.close(descriptor)
    except ProfileImportWriteError:
        raise
    except OSError as exc:
        raise ProfileImportWriteError(
            f"cannot restrict private profile import path: {path}"
        ) from exc


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def image_hash(*, exists: bool, content: str) -> str:
    marker = b"present\0" if exists else b"absent\0"
    return hashlib.sha256(marker + content.encode("utf-8")).hexdigest()


def stable_hash(parts: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def root_identity_hash(root: Path) -> str:
    """Hash the resolved root spelling plus its no-follow filesystem identity."""

    candidate = root.expanduser().resolve(strict=False)
    try:
        value = os.lstat(native_io_path(candidate))
    except OSError as exc:
        raise ProfileImportWriteError(
            f"cannot inspect profile import root: {candidate}"
        ) from exc
    if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
        raise ProfileImportWriteError(
            f"profile import root is not a real directory: {candidate}"
        )
    normalized = os.path.normcase(os.path.normpath(str(candidate)))
    return stable_hash((normalized, str(value.st_dev), str(value.st_ino)))


def _lexical_relative(root: Path, target: Path) -> Path:
    root_abs = root.expanduser().absolute()
    target_abs = target.expanduser().absolute()
    try:
        relative = target_abs.relative_to(root_abs)
    except ValueError as exc:
        raise ProfileImportWriteError(
            f"profile import target is outside its fixed root: {target}"
        ) from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ProfileImportWriteError(f"profile import target is not a safe child path: {target}")
    return relative


def assert_safe_target(root: Path, target: Path) -> None:
    """Reject symlink traversal beneath an already-resolved configured root."""

    relative = _lexical_relative(root, target)
    current = root.expanduser().absolute()
    if os.path.lexists(native_io_path(current)):
        try:
            root_value = os.lstat(native_io_path(current))
        except OSError as exc:
            raise ProfileImportWriteError(
                f"cannot inspect profile import root: {current}"
            ) from exc
        if _is_link_or_reparse(root_value) or not stat.S_ISDIR(root_value.st_mode):
            raise ProfileImportWriteError(
                f"profile import root is not a real directory: {current}"
            )
    for index, part in enumerate(relative.parts):
        current = current / part
        if not os.path.lexists(native_io_path(current)):
            continue
        try:
            value = os.lstat(native_io_path(current))
        except OSError as exc:
            raise ProfileImportWriteError(
                f"cannot inspect profile import target: {current}"
            ) from exc
        if _is_link_or_reparse(value):
            raise ProfileImportWriteError(
                f"profile import refuses a link or reparse-point target: {current}"
            )
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(value.st_mode):
            raise ProfileImportWriteError(
                f"profile import target parent is not a directory: {current}"
            )


def ensure_safe_parent(root: Path, target: Path, *, private: bool = False) -> None:
    """Create missing parent components without following symlinks."""

    relative = _lexical_relative(root, target)
    if private and relative.parts[0] != "profile-imports":
        raise ProfileImportWriteError(
            "private profile import state must remain under profile-imports"
        )
    current = root.expanduser().absolute()
    if not os.path.lexists(native_io_path(current)):
        os.makedirs(
            native_io_path(current),
            mode=0o700 if private else 0o755,
            exist_ok=True,
        )
    root_value = os.lstat(native_io_path(current))
    if _is_link_or_reparse(root_value) or not stat.S_ISDIR(root_value.st_mode):
        raise ProfileImportWriteError(
            f"profile import root is not a real directory: {current}"
        )
    for part in relative.parts[:-1]:
        current = current / part
        if os.path.lexists(native_io_path(current)):
            value = os.lstat(native_io_path(current))
            if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
                raise ProfileImportWriteError(f"unsafe profile import directory: {current}")
            if private:
                restrict_private_path(current, directory=True)
            continue
        try:
            if private and os.name == "nt":
                _create_windows_private_directory(current)
            else:
                os.mkdir(native_io_path(current), 0o700 if private else 0o755)
        except FileExistsError:
            value = os.lstat(native_io_path(current))
            if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
                raise ProfileImportWriteError(f"unsafe profile import directory: {current}")
        if private:
            restrict_private_path(current, directory=True)
    assert_safe_target(root, target)


def read_text_image(root: Path, target: Path) -> tuple[bool, str, int | None]:
    assert_safe_target(root, target)
    try:
        snapshot = capture_bound_profile_file(root, target)
        if snapshot is None:
            return False, "", None
        return True, snapshot.data.decode("utf-8"), snapshot.mode
    except UnicodeDecodeError as exc:
        raise ProfileImportWriteError(f"profile import target is not UTF-8: {target}") from exc
    except (BoundProfileReadError, OSError) as exc:
        raise ProfileImportWriteError(f"cannot read profile import target: {target}") from exc


def canonicalize_replacement(before: str, candidate: str, *, existed: bool) -> str:
    """Preserve BOM, dominant newline style, and trailing-newline state."""

    value = candidate
    if existed:
        before_has_bom = before.startswith("\ufeff")
        value = value.lstrip("\ufeff")
        if before_has_bom:
            value = "\ufeff" + value

        before_body = before[1:] if before_has_bom else before
        candidate_body = value[1:] if before_has_bom else value
        crlf_count = before_body.count("\r\n")
        lone_lf_count = before_body.count("\n") - crlf_count
        newline = "\r\n" if crlf_count > lone_lf_count else "\n"
        candidate_body = candidate_body.replace("\r\n", "\n").replace("\r", "\n")
        if newline == "\r\n":
            candidate_body = candidate_body.replace("\n", "\r\n")

        had_trailing_newline = before_body.endswith(("\n", "\r"))
        candidate_body = candidate_body.rstrip("\r\n")
        if had_trailing_newline:
            candidate_body += newline
        value = ("\ufeff" if before_has_bom else "") + candidate_body
    else:
        value = value.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    return value


def _unified_diff(
    before: str,
    after: str,
    *,
    relative_path: str,
) -> tuple[str, int, int]:
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
            lineterm="\n",
        )
    )
    additions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return "".join(lines), additions, deletions


def _make_plan(
    *,
    root: Path,
    path: Path,
    root_kind: Literal["agent_workspace", "memory_workspace"],
    target: DecisionTarget,
    display_name: str,
    relative_path: str,
    candidate: str,
) -> tuple[InternalFilePlan | None, str]:
    before_exists, before, _mode = read_text_image(root, path)
    after = canonicalize_replacement(before, candidate, existed=before_exists)
    before_digest = image_hash(exists=before_exists, content=before)
    if not before_exists and not after:
        return None, before_digest
    if before_exists and before == after:
        return None, before_digest
    status = FileChangeStatus.CREATED if not before_exists else FileChangeStatus.MODIFIED
    diff, additions, deletions = _unified_diff(before, after, relative_path=relative_path)
    return (
        InternalFilePlan(
            target=target,
            display_name=display_name,
            relative_path=relative_path,
            root_kind=root_kind,
            before_exists=before_exists,
            before_content=before,
            before_hash=before_digest,
            after_exists=True,
            after_content=after,
            after_hash=image_hash(exists=True, content=after),
            status=status,
            additions=additions,
            deletions=deletions,
            diff=diff,
        ),
        before_digest,
    )


def build_file_plans(
    paths: ProfileImportPaths,
    *,
    batch_id: str,
    output: FusionOutput,
) -> tuple[list[InternalFilePlan], str, str]:
    """Map logical model targets onto the three fixed server-owned paths."""

    plans: list[InternalFilePlan] = []
    user_plan, user_hash = _make_plan(
        root=paths.agent_workspace_dir,
        path=paths.user_path,
        root_kind="agent_workspace",
        target=DecisionTarget.USER,
        display_name="Personal profile",
        relative_path="USER.md",
        candidate=output.candidate.user_md,
    )
    if user_plan is not None:
        plans.append(user_plan)

    memory_plan, memory_hash = _make_plan(
        root=paths.memory_workspace_dir,
        path=paths.memory_path,
        root_kind="memory_workspace",
        target=DecisionTarget.MEMORY,
        display_name="Long-term preferences",
        relative_path="MEMORY.md",
        candidate=output.candidate.memory_md,
    )
    if memory_plan is not None:
        plans.append(memory_plan)

    import_candidate = output.candidate.import_md
    if import_candidate is not None and import_candidate.strip():
        relative_path = f"memory/imports/{batch_id}.md"
        import_path = paths.imports_dir / f"{batch_id}.md"
        import_plan, _import_hash = _make_plan(
            root=paths.memory_workspace_dir,
            path=import_path,
            root_kind="memory_workspace",
            target=DecisionTarget.IMPORT,
            display_name="Project records",
            relative_path=relative_path,
            candidate=import_candidate,
        )
        if import_plan is not None:
            plans.append(import_plan)
    return plans, user_hash, memory_hash


def _make_undo_review_plan(
    *,
    root: Path,
    path: Path,
    root_kind: Literal["agent_workspace", "memory_workspace"],
    target: DecisionTarget,
    display_name: str,
    relative_path: str,
    candidate: str | None,
) -> tuple[InternalFilePlan | None, str]:
    before_exists, before, _mode = read_text_image(root, path)
    before_digest = image_hash(exists=before_exists, content=before)
    after_exists = candidate is not None
    after = (
        canonicalize_replacement(before, candidate, existed=before_exists)
        if candidate is not None
        else ""
    )
    if before_exists == after_exists and before == after:
        return None, before_digest
    status = (
        FileChangeStatus.DELETED
        if not after_exists
        else FileChangeStatus.CREATED
        if not before_exists
        else FileChangeStatus.MODIFIED
    )
    diff, additions, deletions = _unified_diff(
        before,
        after if after_exists else "",
        relative_path=relative_path,
    )
    return (
        InternalFilePlan(
            target=target,
            display_name=display_name,
            relative_path=relative_path,
            root_kind=root_kind,
            before_exists=before_exists,
            before_content=before,
            before_hash=before_digest,
            after_exists=after_exists,
            after_content=after,
            after_hash=image_hash(exists=after_exists, content=after),
            status=status,
            additions=additions,
            deletions=deletions,
            diff=diff,
        ),
        before_digest,
    )


def build_undo_review_file_plans(
    paths: ProfileImportPaths,
    *,
    receipt: ProfileImportReceipt,
    output: FusionOutput,
) -> tuple[list[InternalFilePlan], str, str]:
    """Map a stale-undo model result only onto targets changed by the receipt."""

    original_targets = {plan.target for plan in receipt.files}
    plans: list[InternalFilePlan] = []
    user_plan, user_hash = _make_undo_review_plan(
        root=paths.agent_workspace_dir,
        path=paths.user_path,
        root_kind="agent_workspace",
        target=DecisionTarget.USER,
        display_name="Personal profile",
        relative_path="USER.md",
        candidate=output.candidate.user_md,
    )
    if user_plan is not None:
        if DecisionTarget.USER not in original_targets:
            raise ProfileImportInvalidOutputError(
                "undo candidate changed USER although the receipt did not"
            )
        plans.append(user_plan)

    memory_plan, memory_hash = _make_undo_review_plan(
        root=paths.memory_workspace_dir,
        path=paths.memory_path,
        root_kind="memory_workspace",
        target=DecisionTarget.MEMORY,
        display_name="Long-term preferences",
        relative_path="MEMORY.md",
        candidate=output.candidate.memory_md,
    )
    if memory_plan is not None:
        if DecisionTarget.MEMORY not in original_targets:
            raise ProfileImportInvalidOutputError(
                "undo candidate changed MEMORY although the receipt did not"
            )
        plans.append(memory_plan)

    original_import = next(
        (plan for plan in receipt.files if plan.target is DecisionTarget.IMPORT),
        None,
    )
    import_candidate = output.candidate.import_md
    if original_import is None:
        if import_candidate is not None and import_candidate.strip():
            raise ProfileImportInvalidOutputError(
                "undo candidate created IMPORT although the receipt did not"
            )
    else:
        import_plan, _import_hash = _make_undo_review_plan(
            root=paths.memory_workspace_dir,
            path=paths.memory_workspace_dir / original_import.relative_path,
            root_kind="memory_workspace",
            target=DecisionTarget.IMPORT,
            display_name="Project records",
            relative_path=original_import.relative_path,
            candidate=import_candidate if import_candidate and import_candidate.strip() else None,
        )
        if import_plan is not None:
            plans.append(import_plan)
    return plans, user_hash, memory_hash


def public_file_diffs(plans: list[InternalFilePlan]) -> list[ProfileImportFileDiff]:
    return [
        ProfileImportFileDiff(
            target=plan.target,
            display_name=plan.display_name,
            relative_path=plan.relative_path,
            status=plan.status,
            additions=plan.additions,
            deletions=plan.deletions,
            diff=plan.diff,
        )
        for plan in plans
    ]


def reverse_file_plans(plans: list[InternalFilePlan]) -> list[InternalFilePlan]:
    """Build exact inverse plans from one applied receipt."""

    result: list[InternalFilePlan] = []
    for plan in plans:
        if plan.before_exists:
            status = FileChangeStatus.MODIFIED
        else:
            status = FileChangeStatus.DELETED
        diff, additions, deletions = _unified_diff(
            plan.after_content if plan.after_exists else "",
            plan.before_content if plan.before_exists else "",
            relative_path=plan.relative_path,
        )
        result.append(
            InternalFilePlan(
                target=plan.target,
                display_name=plan.display_name,
                relative_path=plan.relative_path,
                root_kind=plan.root_kind,
                before_exists=plan.after_exists,
                before_content=plan.after_content,
                before_hash=plan.after_hash,
                after_exists=plan.before_exists,
                after_content=plan.before_content,
                after_hash=plan.before_hash,
                status=status,
                additions=additions,
                deletions=deletions,
                diff=diff,
            )
        )
    return result


def target_path(paths: ProfileImportPaths, plan: InternalFilePlan) -> tuple[Path, Path]:
    if plan.root_kind == "agent_workspace":
        root = paths.agent_workspace_dir
    else:
        root = paths.memory_workspace_dir
    candidate = root / Path(plan.relative_path)
    assert_safe_target(root, candidate)
    return root, candidate


def history_snapshot(paths: ProfileImportPaths) -> tuple[list[dict[str, str]], str]:
    """Read existing imports newest-first and return a deterministic aggregate hash."""

    assert_safe_target(paths.memory_workspace_dir, paths.imports_dir / "_probe")
    entries: list[tuple[int, str, str]] = []
    try:
        captured = capture_bound_profile_directory(
            paths.memory_workspace_dir,
            paths.imports_dir,
            suffix=".md",
        )
    except (BoundProfileReadError, OSError) as exc:
        raise ProfileImportWriteError("cannot enumerate existing profile imports") from exc
    if captured is None:
        return [], stable_hash([])
    for name, snapshot in captured:
        try:
            content = snapshot.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProfileImportWriteError(
                f"cannot read existing profile import: {paths.imports_dir / name}"
            ) from exc
        entries.append((snapshot.mtime_ns, name, content))
    entries.sort(key=lambda item: (item[0], item[1]), reverse=True)
    history = [{"name": name, "content": content} for _mtime, name, content in entries]
    digest = stable_hash(
        part
        for _mtime, name, content in entries
        for part in (name, image_hash(exists=True, content=content))
    )
    return history, digest


def enforce_quotas(
    paths: ProfileImportPaths,
    plans: list[InternalFilePlan],
    quotas: ProfileImportQuotas,
) -> None:
    for plan in plans:
        size = len(plan.after_content.encode("utf-8"))
        if quotas.max_file_size_kb and size > quotas.max_file_size_kb * 1024:
            raise ProfileImportInvalidOutputError(
                f"{plan.relative_path} exceeds the configured per-file memory limit"
            )

    memory_files: dict[str, int] = {}
    candidates = [paths.memory_path]
    memory_dir = paths.memory_workspace_dir / "memory"
    if memory_dir.exists():
        candidates.extend(memory_dir.rglob("*.md"))
    for path in candidates:
        try:
            assert_safe_target(paths.memory_workspace_dir, path)
            relative = path.relative_to(paths.memory_workspace_dir)
            if any(part.startswith(".") for part in relative.parts):
                continue
            if not os.path.lexists(native_io_path(path)):
                continue
            value = os.lstat(native_io_path(path))
            if _is_link_or_reparse(value):
                continue
            if stat.S_ISREG(value.st_mode):
                memory_files[str(path.absolute())] = value.st_size
        except OSError as exc:
            raise ProfileImportWriteError("cannot calculate memory quotas") from exc

    for plan in plans:
        if plan.root_kind != "memory_workspace":
            continue
        _root, path = target_path(paths, plan)
        key = str(path.absolute())
        if plan.after_exists:
            memory_files[key] = len(plan.after_content.encode("utf-8"))
        else:
            memory_files.pop(key, None)

    if quotas.max_files and len(memory_files) > quotas.max_files:
        raise ProfileImportInvalidOutputError("profile import would exceed the memory file limit")
    if quotas.max_total_size_kb and sum(memory_files.values()) > quotas.max_total_size_kb * 1024:
        raise ProfileImportInvalidOutputError(
            "profile import would exceed the total memory size limit"
        )
