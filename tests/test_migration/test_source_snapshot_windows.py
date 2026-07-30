from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.migration import opensquilla_home as home_migration
from opensquilla.migration import source_snapshot_windows as windows_snapshot


def _information(
    inode: int,
    *,
    directory: bool,
    size: int = 0,
    attributes: int = 0,
) -> windows_snapshot._HandleInformation:
    if directory:
        attributes |= windows_snapshot._FILE_ATTRIBUTE_DIRECTORY
        file_type = stat.S_IFDIR
    else:
        file_type = stat.S_IFREG
    return windows_snapshot._HandleInformation(
        identity=windows_snapshot.WindowsPathIdentity(
            device=71,
            inode=inode,
            file_type=file_type,
        ),
        mode=windows_snapshot._mode_from_attributes(attributes),
        size=size,
        mtime_ns=1_700_000_000_000_000_000 + inode,
        attributes=attributes,
    )


class _FakeWindowsApi:
    def __init__(self) -> None:
        self.root = Path("/fake/source")
        self.data = {
            self.root / "state" / "sessions.db": b"database",
            self.root / "state" / "sessions.db-wal": b"write-ahead-log",
            self.root / "state" / "sessions.db-shm": b"shared-memory",
        }
        self.nodes = {
            Path("/"): _information(1, directory=True),
            Path("/fake"): _information(2, directory=True),
            self.root: _information(3, directory=True),
            self.root / "state": _information(4, directory=True),
            **{
                path: _information(index, directory=False, size=len(value))
                for index, (path, value) in enumerate(self.data.items(), start=5)
            },
        }
        self.children = {
            Path("/"): ("fake",),
            Path("/fake"): ("source",),
            self.root: ("state",),
            self.root / "state": tuple(path.name for path in self.data),
        }
        self._next_handle = 100
        self._handles: dict[int, Path] = {}
        self._offsets: dict[int, int] = {}
        self.open_order: list[Path] = []
        self.open_modes: list[tuple[Path, bool]] = []
        self.read_paths: list[Path] = []

    def normalize_root(self, path: Path) -> Path:
        assert path == self.root or self.root in path.parents
        return path

    def open_path(self, path: Path, *, allow_writers: bool) -> int:
        path = Path(path)
        assert path in self.nodes
        if path != Path("/"):
            parent = path.parent
            assert parent in self._handles.values(), f"parent was not pinned: {parent}"
        handle = self._next_handle
        self._next_handle += 1
        self._handles[handle] = path
        self._offsets[handle] = 0
        self.open_order.append(path)
        self.open_modes.append((path, allow_writers))
        return handle

    def close(self, handle: int) -> None:
        self._handles.pop(handle)
        self._offsets.pop(handle)

    def information(
        self,
        handle: int,
        *,
        path: Path,
    ) -> windows_snapshot._HandleInformation:
        assert self._handles[handle] == path
        return self.nodes[path]

    def enumerate_names(self, handle: int, *, path: Path) -> tuple[str, ...]:
        assert self._handles[handle] == path
        return tuple(sorted(self.children.get(path, ())))

    def read(self, handle: int, size: int, *, path: Path) -> bytes:
        assert self._handles[handle] == path
        self.read_paths.append(path)
        data = self.data[path]
        offset = self._offsets[handle]
        chunk = data[offset : offset + size]
        self._offsets[handle] += len(chunk)
        return chunk

    @property
    def open_handles(self) -> set[int]:
        return set(self._handles)


def _add_gateway_authority(api: _FakeWindowsApi) -> None:
    values = {
        api.root / "state" / "gateway.pid": b"12345\n",
        api.root / "state" / "gateway.pid.lock": b"locked\n",
    }
    start = max(value.identity.inode for value in api.nodes.values()) + 1
    for index, (path, value) in enumerate(values.items(), start=start):
        api.data[path] = value
        api.nodes[path] = _information(index, directory=False, size=len(value))
    api.children[api.root / "state"] = tuple(
        sorted({*api.children[api.root / "state"], *(path.name for path in values)})
    )


def _add_reparse_descendant(
    api: _FakeWindowsApi,
    *,
    top_level: str,
) -> Path:
    """Add a representative generated tree ending in a Windows reparse point."""

    top = api.root / top_level
    repository = top / "run-1" / "repo"
    reparse = repository / ".venv" / "Scripts" / "python.exe"
    directories = (
        top,
        top / "run-1",
        repository,
        repository / ".venv",
        repository / ".venv" / "Scripts",
    )
    next_inode = max(value.identity.inode for value in api.nodes.values()) + 1
    for offset, directory in enumerate(directories):
        api.nodes[directory] = _information(next_inode + offset, directory=True)
    api.nodes[reparse] = _information(
        next_inode + len(directories),
        directory=False,
        attributes=windows_snapshot._FILE_ATTRIBUTE_REPARSE_POINT,
    )
    api.children[api.root] = tuple(sorted({*api.children[api.root], top_level}))
    api.children[top] = ("run-1",)
    api.children[top / "run-1"] = ("repo",)
    api.children[repository] = (".venv",)
    api.children[repository / ".venv"] = ("Scripts",)
    api.children[repository / ".venv" / "Scripts"] = ("python.exe",)
    return reparse


def test_public_scan_and_copy_use_pinned_api_for_sqlite_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scan_api = _FakeWindowsApi()
    monkeypatch.setattr(windows_snapshot, "_new_api", lambda: scan_api)

    snapshot = windows_snapshot.scan_windows_source_tree(
        scan_api.root,
        destination_prefix=Path(),
        role="primary",
    )

    assert not scan_api.open_handles
    assert (Path("/"), True) in scan_api.open_modes
    assert (Path("/fake"), True) in scan_api.open_modes
    assert (scan_api.root, False) in scan_api.open_modes
    assert (scan_api.root / "state", True) in scan_api.open_modes
    assert (scan_api.root / "state", False) in scan_api.open_modes
    files = [entry for entry in snapshot.entries if entry.entry_type == "file"]
    assert {entry.relative.as_posix() for entry in files} == {
        "state/sessions.db",
        "state/sessions.db-shm",
        "state/sessions.db-wal",
    }
    for entry in files:
        assert entry.digest == hashlib.sha256(scan_api.data[entry.source]).hexdigest()
        assert (entry.source, True) in scan_api.open_modes

    for entry in files:
        copy_api = _FakeWindowsApi()
        monkeypatch.setattr(windows_snapshot, "_new_api", lambda api=copy_api: api)
        destination = tmp_path / entry.relative
        digest = windows_snapshot.copy_windows_snapshot_file(snapshot, entry, destination)
        assert digest == entry.digest
        assert destination.read_bytes() == copy_api.data[entry.source]
        assert (entry.source, True) in copy_api.open_modes
        assert not copy_api.open_handles


def test_scan_excludes_transient_sqlite_shm_without_reading_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    shm = api.root / "state" / "sessions.db-shm"
    monkeypatch.setattr(windows_snapshot, "_new_api", lambda: api)

    snapshot = windows_snapshot.scan_windows_source_tree(
        api.root,
        destination_prefix=Path(),
        role="primary",
        excluded_leaf_suffixes=("-shm",),
    )

    files = {
        entry.relative.as_posix()
        for entry in snapshot.entries
        if entry.entry_type == "file"
    }
    assert files == {"state/sessions.db", "state/sessions.db-wal"}
    assert shm not in api.read_paths
    assert snapshot.excluded_leaf_suffixes == ("-shm",)


@pytest.mark.parametrize("top_level", ["code-task", "profiles"])
def test_scan_excludes_non_imported_tree_before_opening_its_reparse_descendant(
    monkeypatch: pytest.MonkeyPatch,
    top_level: str,
) -> None:
    api = _FakeWindowsApi()
    reparse = _add_reparse_descendant(api, top_level=top_level)
    excluded_root = api.root / top_level
    monkeypatch.setattr(windows_snapshot, "_new_api", lambda: api)

    snapshot = windows_snapshot.scan_windows_source_tree(
        api.root,
        destination_prefix=Path(),
        role="primary",
        excluded={Path(top_level)},
    )

    assert snapshot.excluded == frozenset({Path(top_level)})
    assert all(
        Path(top_level) not in entry.relative.parents
        and entry.relative != Path(top_level)
        for entry in snapshot.entries
    )
    assert excluded_root not in api.open_order
    assert reparse not in api.open_order
    assert not api.open_handles


def test_scan_still_rejects_reparse_outside_excluded_code_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    _add_reparse_descendant(api, top_level="code-task")
    workspace_reparse = _add_reparse_descendant(api, top_level="workspace")
    monkeypatch.setattr(windows_snapshot, "_new_api", lambda: api)

    with pytest.raises(
        windows_snapshot.WindowsSourceSnapshotError,
        match="reparse point",
    ):
        windows_snapshot.scan_windows_source_tree(
            api.root,
            destination_prefix=Path(),
            role="primary",
            excluded={Path("code-task")},
        )

    assert api.root / "code-task" not in api.open_order
    assert workspace_reparse in api.open_order
    assert not api.open_handles


def test_win32_read_error_keeps_the_specific_source_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("C:/portable/state/sessions.db")
    monkeypatch.setattr(windows_snapshot.ctypes, "get_last_error", lambda: 33, raising=False)

    with pytest.raises(windows_snapshot.WindowsSourceSnapshotError) as captured:
        windows_snapshot._raise_windows_error("ReadFile", source)

    assert captured.value.errno == 33
    assert captured.value.filename == str(source)
    assert "ReadFile failed (WinError 33)" in str(captured.value)


def test_parent_reparse_swap_is_rejected_before_sqlite_leaf_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scan_api = _FakeWindowsApi()
    monkeypatch.setattr(windows_snapshot, "_new_api", lambda: scan_api)
    snapshot = windows_snapshot.scan_windows_source_tree(
        scan_api.root,
        destination_prefix=Path(),
        role="primary",
    )
    database = next(
        entry for entry in snapshot.entries if entry.relative == Path("state/sessions.db")
    )

    swapped_api = _FakeWindowsApi()
    old_state = swapped_api.nodes[swapped_api.root / "state"]
    swapped_api.nodes[swapped_api.root / "state"] = windows_snapshot._HandleInformation(
        identity=old_state.identity,
        mode=old_state.mode,
        size=old_state.size,
        mtime_ns=old_state.mtime_ns,
        attributes=old_state.attributes | windows_snapshot._FILE_ATTRIBUTE_REPARSE_POINT,
    )
    monkeypatch.setattr(windows_snapshot, "_new_api", lambda: swapped_api)
    destination = tmp_path / "sessions.db"

    with pytest.raises(
        windows_snapshot.WindowsSourceSnapshotError,
        match="reparse point",
    ):
        windows_snapshot.copy_windows_snapshot_file(snapshot, database, destination)

    assert not destination.exists()
    assert swapped_api.root / "state" / "sessions.db" not in swapped_api.read_paths
    assert not swapped_api.open_handles


def test_file_writer_sharing_still_rejects_metadata_change_during_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    database = api.root / "state" / "sessions.db"
    original_read = api.read
    mutated = False

    def mutate_after_first_read(handle: int, size: int, *, path: Path) -> bytes:
        nonlocal mutated
        chunk = original_read(handle, size, path=path)
        if api._handles[handle] == database and not mutated:
            current = api.nodes[database]
            api.nodes[database] = windows_snapshot._HandleInformation(
                identity=current.identity,
                mode=current.mode,
                size=current.size,
                mtime_ns=current.mtime_ns + 100,
                attributes=current.attributes,
            )
            mutated = True
        return chunk

    api.read = mutate_after_first_read  # type: ignore[method-assign]
    monkeypatch.setattr(windows_snapshot, "_new_api", lambda: api)

    with pytest.raises(
        windows_snapshot.WindowsSourceSnapshotError,
        match="source changed while pinned",
    ):
        windows_snapshot.scan_windows_source_tree(
            api.root,
            destination_prefix=Path(),
            role="primary",
        )

    assert (database, True) in api.open_modes
    assert not api.open_handles


def test_plain_parent_directory_swap_is_rejected_before_leaf_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scan_api = _FakeWindowsApi()
    monkeypatch.setattr(windows_snapshot, "_new_api", lambda: scan_api)
    snapshot = windows_snapshot.scan_windows_source_tree(
        scan_api.root,
        destination_prefix=Path(),
        role="primary",
    )
    database = next(
        entry for entry in snapshot.entries if entry.relative == Path("state/sessions.db")
    )

    swapped_api = _FakeWindowsApi()
    swapped_api.nodes[swapped_api.root / "state"] = _information(9001, directory=True)
    monkeypatch.setattr(windows_snapshot, "_new_api", lambda: swapped_api)
    destination = tmp_path / "sessions.db"

    with pytest.raises(
        windows_snapshot.WindowsSourceSnapshotError,
        match="source directory changed before copy",
    ):
        windows_snapshot.copy_windows_snapshot_file(snapshot, database, destination)

    assert not destination.exists()
    assert swapped_api.root / "state" / "sessions.db" not in swapped_api.read_paths
    assert not swapped_api.open_handles


def test_bounded_gateway_authority_uses_same_pinned_parent_chain() -> None:
    api = _FakeWindowsApi()
    _add_gateway_authority(api)

    captured = windows_snapshot._capture_bounded_with_api(
        api,
        api.root / "state",
        names=("gateway.pid", "gateway.pid.lock"),
        max_bytes=1024,
    )

    assert captured is not None
    assert captured.file("gateway.pid") is not None
    assert captured.file("gateway.pid").data == b"12345\n"
    assert captured.file("gateway.pid.lock") is not None
    assert captured.file("gateway.pid.lock").data == b"locked\n"
    assert (api.root / "state" / "gateway.pid", False) in api.open_modes
    assert (api.root / "state" / "gateway.pid.lock", False) in api.open_modes
    assert not api.open_handles


def test_pinned_ancestor_ignores_metadata_from_unrelated_sibling_activity() -> None:
    api = _FakeWindowsApi()
    ancestor = Path("/fake")

    with windows_snapshot._open_directory_chain(api, api.root):
        original = api.nodes[ancestor]
        api.nodes[ancestor] = windows_snapshot._HandleInformation(
            identity=original.identity,
            mode=original.mode,
            size=original.size + 1,
            mtime_ns=original.mtime_ns + 1,
            attributes=original.attributes,
        )

    assert not api.open_handles


def test_pinned_ancestor_still_rejects_identity_replacement() -> None:
    api = _FakeWindowsApi()
    ancestor = Path("/fake")

    with pytest.raises(
        windows_snapshot.WindowsSourceSnapshotError,
        match="source path component changed",
    ):
        with windows_snapshot._open_directory_chain(api, api.root):
            api.nodes[ancestor] = _information(9001, directory=True)

    assert not api.open_handles


def test_bounded_gateway_authority_rejects_state_parent_reparse() -> None:
    api = _FakeWindowsApi()
    _add_gateway_authority(api)
    old_state = api.nodes[api.root / "state"]
    api.nodes[api.root / "state"] = windows_snapshot._HandleInformation(
        identity=old_state.identity,
        mode=old_state.mode,
        size=old_state.size,
        mtime_ns=old_state.mtime_ns,
        attributes=old_state.attributes | windows_snapshot._FILE_ATTRIBUTE_REPARSE_POINT,
    )

    with pytest.raises(windows_snapshot.WindowsSourceSnapshotError, match="reparse point"):
        windows_snapshot._capture_bounded_with_api(
            api,
            api.root / "state",
            names=("gateway.pid", "gateway.pid.lock"),
            max_bytes=1024,
        )

    assert not api.open_handles


def test_migration_dispatch_retains_native_windows_snapshot_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _FakeWindowsApi()
    native = windows_snapshot._scan_with_api(
        api,
        api.root,
        destination_prefix=Path(),
        role="primary",
        excluded=frozenset(),
        excluded_leaf_suffixes=(),
    )
    monkeypatch.setattr(home_migration, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(home_migration, "scan_windows_source_tree", lambda *args, **kwargs: native)

    snapshot = home_migration._scan_source_tree(
        api.root,
        destination_prefix=Path(),
        role="primary",
    )

    assert snapshot.windows_snapshot is native
    entry = next(item for item in snapshot.entries if item.relative == Path("state/sessions.db"))
    observed: dict[str, object] = {}

    def copy_native(
        received_snapshot: windows_snapshot.WindowsSourceSnapshot,
        received_entry: windows_snapshot.WindowsManifestEntry,
        destination: Path,
    ) -> str:
        observed.update(
            snapshot=received_snapshot,
            entry=received_entry,
            destination=destination,
        )
        return received_entry.digest or ""

    monkeypatch.setattr(home_migration, "copy_windows_snapshot_file", copy_native)
    destination = tmp_path / "sessions.db"
    digest = home_migration._copy_snapshot_file(snapshot, entry, destination)

    assert digest == entry.digest
    assert observed == {
        "snapshot": native,
        "entry": next(item for item in native.entries if item.relative == entry.relative),
        "destination": destination,
    }


def test_gateway_authority_dispatch_retains_native_windows_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    _add_gateway_authority(api)
    native = windows_snapshot._capture_bounded_with_api(
        api,
        api.root / "state",
        names=("gateway.pid", "gateway.pid.lock"),
        max_bytes=1024,
    )
    assert native is not None
    monkeypatch.setattr(home_migration, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        home_migration,
        "capture_windows_bounded_files",
        lambda *args, **kwargs: native,
    )

    captured = home_migration._capture_legacy_gateway_authority(api.root / "state")

    assert captured.windows_authority is native
    assert captured.pid_value == 12345
    assert captured.pid is not None
    assert captured.lock is not None


def test_createfile_contract_excludes_delete_share_and_opens_reparse_object() -> None:
    api = object.__new__(windows_snapshot._Win32SourceApi)
    captured: dict[str, Any] = {}

    def create_file(*args: Any) -> int:
        captured["args"] = args
        return 321

    api._create_file = create_file

    assert api.open_path(Path("C:/synthetic/profile"), allow_writers=False) == 321
    args = captured["args"]
    assert args[2] == windows_snapshot._FILE_SHARE_READ
    assert not args[2] & windows_snapshot._FILE_SHARE_DELETE
    assert args[5] & windows_snapshot._FILE_FLAG_OPEN_REPARSE_POINT
    assert args[5] & windows_snapshot._FILE_FLAG_BACKUP_SEMANTICS

    assert api.open_path(Path("C:/synthetic"), allow_writers=True) == 321
    directory_args = captured["args"]
    assert directory_args[2] & windows_snapshot._FILE_SHARE_WRITE
    assert not directory_args[2] & windows_snapshot._FILE_SHARE_DELETE


def test_directory_enumeration_header_uses_fixed_width_windows_layout() -> None:
    assert windows_snapshot._FileIdBothDirInfoHeader.file_name.offset == 104


@pytest.mark.skipif(os.name == "nt", reason="non-Windows fail-closed contract")
def test_native_api_has_no_non_windows_path_fallback() -> None:
    with pytest.raises(windows_snapshot.WindowsSourceSnapshotUnavailableError):
        windows_snapshot._new_api()


@pytest.mark.skipif(os.name != "nt", reason="requires real Win32 source handles")
def test_real_windows_snapshot_copies_database_sidecars(tmp_path: Path) -> None:
    source = tmp_path / "source"
    state = source / "state"
    state.mkdir(parents=True)
    values = {
        "sessions.db": b"db",
        "sessions.db-wal": b"wal",
        "sessions.db-shm": b"shm",
    }
    for name, value in values.items():
        (state / name).write_bytes(value)

    snapshot = windows_snapshot.scan_windows_source_tree(
        source,
        destination_prefix=Path(),
        role="primary",
    )
    for entry in snapshot.entries:
        if entry.entry_type != "file":
            continue
        destination = tmp_path / "copied" / entry.relative
        windows_snapshot.copy_windows_snapshot_file(snapshot, entry, destination)
        assert destination.read_bytes() == values[entry.relative.name]


@pytest.mark.skipif(os.name != "nt", reason="requires real Win32 source handles")
def test_real_windows_active_sqlite_shm_is_ignored_and_wal_is_preserved(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-active-sqlite"
    state = source / "state"
    state.mkdir(parents=True)
    database = state / "sessions.db"
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE sessions (value TEXT NOT NULL)")
        writer.execute("INSERT INTO sessions VALUES ('committed-in-wal')")
        writer.commit()
        shm = database.with_name("sessions.db-shm")
        wal = database.with_name("sessions.db-wal")
        assert shm.is_file()
        assert wal.stat().st_size > 32

        started = time.monotonic()
        snapshot = windows_snapshot.scan_windows_source_tree(
            source,
            destination_prefix=Path(),
            role="primary",
            excluded_leaf_suffixes=("-shm",),
        )
        assert time.monotonic() - started < 5
        files = {
            entry.relative.as_posix()
            for entry in snapshot.entries
            if entry.entry_type == "file"
        }
        assert files == {"state/sessions.db", "state/sessions.db-wal"}

        copied = tmp_path / "copied-active-sqlite"
        for entry in snapshot.entries:
            if entry.entry_type == "file":
                windows_snapshot.copy_windows_snapshot_file(
                    snapshot,
                    entry,
                    copied / entry.relative,
                )
        reader = sqlite3.connect(copied / "state" / "sessions.db")
        try:
            assert reader.execute("SELECT value FROM sessions").fetchall() == [
                ("committed-in-wal",)
            ]
        finally:
            reader.close()
    finally:
        writer.close()


@pytest.mark.skipif(os.name != "nt", reason="requires real Win32 byte-range locks")
def test_real_windows_persistent_file_lock_reports_path_quickly(
    tmp_path: Path,
) -> None:
    import msvcrt

    source = tmp_path / "source-locked"
    source.mkdir()
    locked = source / "persistent.bin"
    locked.write_bytes(b"locked-data")
    with locked.open("r+b") as owner:
        msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            started = time.monotonic()
            with pytest.raises(windows_snapshot.WindowsSourceSnapshotError) as captured:
                windows_snapshot.scan_windows_source_tree(
                    source,
                    destination_prefix=Path(),
                    role="primary",
                    excluded_leaf_suffixes=("-shm",),
                )
            assert time.monotonic() - started < 5
            assert captured.value.errno in {32, 33}
            assert captured.value.filename == str(locked)
        finally:
            owner.seek(0)
            msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)


@pytest.mark.skipif(os.name != "nt", reason="requires real Win32 source handles")
def test_real_windows_pinned_ancestor_allows_unrelated_sibling_write(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    api = windows_snapshot._new_api()

    with windows_snapshot._open_directory_chain(api, source):
        sibling = tmp_path / "unrelated-sibling.txt"
        sibling.write_bytes(b"unrelated")

    assert sibling.read_bytes() == b"unrelated"


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction or symlink")
def test_real_windows_parent_reparse_to_original_directory_is_rejected(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    state = source / "state"
    state.mkdir(parents=True)
    database = state / "sessions.db"
    database.write_bytes(b"stable-database")
    snapshot = windows_snapshot.scan_windows_source_tree(
        source,
        destination_prefix=Path(),
        role="primary",
    )
    entry = next(item for item in snapshot.entries if item.relative == Path("state/sessions.db"))

    moved = source / "moved-state"
    state.rename(moved)
    try:
        state.symlink_to(moved, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"creating a directory reparse point is unavailable: {exc}")

    destination = tmp_path / "copied.db"
    with pytest.raises(
        windows_snapshot.WindowsSourceSnapshotError,
        match="reparse point|source root changed",
    ):
        windows_snapshot.copy_windows_snapshot_file(snapshot, entry, destination)
    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction or symlink")
def test_real_windows_gateway_authority_parent_reparse_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    state = source / "state"
    state.mkdir(parents=True)
    (state / "gateway.pid").write_text("12345\n", encoding="utf-8")
    captured = windows_snapshot.capture_windows_bounded_files(
        state,
        names=("gateway.pid", "gateway.pid.lock"),
        max_bytes=1024,
    )
    assert captured is not None

    moved = source / "moved-state"
    state.rename(moved)
    try:
        state.symlink_to(moved, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"creating a directory reparse point is unavailable: {exc}")

    with pytest.raises(windows_snapshot.WindowsSourceSnapshotError, match="reparse point"):
        windows_snapshot.capture_windows_bounded_files(
            state,
            names=("gateway.pid", "gateway.pid.lock"),
            max_bytes=1024,
        )
