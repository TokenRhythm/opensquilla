from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from opensquilla.recovery import engine
from opensquilla.recovery.atomic import _native_io_path

_DATABASE_CASES = [
    ("valid", None),
    ("corrupt", "state_database_invalid"),
    ("future-schema", "state_schema_too_new"),
]


def _database(path: Path, kind: str) -> None:
    if kind == "corrupt":
        path.write_bytes(b"not a sqlite database")
        return
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE user_data (id INTEGER PRIMARY KEY)")
        if kind == "future-schema":
            connection.execute("CREATE TABLE _yoyo_migration (migration_id TEXT)")
            connection.execute("INSERT INTO _yoyo_migration VALUES ('V999__future_cleanup_test')")
        connection.commit()
    finally:
        connection.close()


def _remove_owned_snapshots(roots: list[Path], temporary_root: Path) -> None:
    for root in roots:
        assert root.parent == temporary_root.resolve()
        if root.exists():
            shutil.rmtree(root)


@pytest.mark.parametrize(("database_kind", "expected_code"), _DATABASE_CASES)
def test_database_inspection_keeps_result_when_snapshot_directory_becomes_nonempty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database_kind: str,
    expected_code: str | None,
) -> None:
    """A real root rmdir error must not mask validity or the unsafe-state code."""
    database = tmp_path / "sessions.db"
    _database(database, database_kind)
    before = (database.read_bytes(), database.stat().st_mtime_ns)
    temporary_root = tmp_path / "private-snapshots"
    temporary_root.mkdir()
    roots: list[Path] = []
    errors: list[OSError] = []
    real_directory = tempfile.TemporaryDirectory
    real_rmdir = os.rmdir

    class LateFileDirectory(real_directory):
        def __init__(self, *args, **kwargs):
            kwargs["dir"] = temporary_root
            super().__init__(*args, **kwargs)
            roots.append(Path(self.name).resolve())

        def __exit__(self, *args):
            root = Path(self.name).resolve()

            def rmdir_after_late_file(path, *rmdir_args, **rmdir_kwargs):
                if Path(path).resolve() == root:
                    # Enumeration has finished. Make the actual OS rmdir fail
                    # with ENOTEMPTY (WinError 145), without raising a fake fault.
                    (root / "late-arrival.txt").write_bytes(b"synthetic cleanup race")
                try:
                    return real_rmdir(path, *rmdir_args, **rmdir_kwargs)
                except OSError as exc:
                    errors.append(exc)
                    raise

            with monkeypatch.context() as hooks:
                hooks.setattr(os, "rmdir", rmdir_after_late_file)
                return super().__exit__(*args)

    try:
        with monkeypatch.context() as hooks:
            hooks.setattr(engine.tempfile, "TemporaryDirectory", LateFileDirectory)
            assert engine._database_safety_code(database) == expected_code
        assert len(errors) == 1
        assert errors[0].errno == errno.ENOTEMPTY
        if os.name == "nt":
            assert errors[0].winerror == 145
        assert roots[0].exists(), "the race should leave the disposable snapshot behind"
        assert (database.read_bytes(), database.stat().st_mtime_ns) == before
        assert not database.with_name(f"{database.name}-shm").exists()
    finally:
        _remove_owned_snapshots(roots, temporary_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel file-sharing contract")
@pytest.mark.parametrize(("database_kind", "expected_code"), _DATABASE_CASES)
def test_database_inspection_keeps_result_when_windows_snapshot_file_is_locked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database_kind: str,
    expected_code: str | None,
) -> None:
    """CreateFileW without DELETE sharing exercises real tempfile cleanup."""
    database = tmp_path / "sessions.db"
    _database(database, database_kind)
    before = (database.read_bytes(), database.stat().st_mtime_ns)
    temporary_root = tmp_path / "private-snapshots"
    temporary_root.mkdir()
    roots: list[Path] = []
    errors: list[OSError] = []
    real_directory = tempfile.TemporaryDirectory
    real_unlink = os.unlink
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
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
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    class LockedFileDirectory(real_directory):
        def __init__(self, *args, **kwargs):
            kwargs["dir"] = temporary_root
            super().__init__(*args, **kwargs)
            roots.append(Path(self.name).resolve())

        def __exit__(self, *args):
            # The product has already closed SQLite. Keep a native read handle
            # open through cleanup, sharing reads/writes but excluding DELETE.
            snapshot_file = Path(self.name) / database.name
            handle = create_file(
                _native_io_path(snapshot_file), 0x80000000, 1 | 2, None, 3, 0x80, None
            )
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())

            def tracked_unlink(path, *unlink_args, **unlink_kwargs):
                try:
                    return real_unlink(path, *unlink_args, **unlink_kwargs)
                except OSError as exc:
                    errors.append(exc)
                    raise

            try:
                with monkeypatch.context() as hooks:
                    hooks.setattr(os, "unlink", tracked_unlink)
                    return super().__exit__(*args)
            finally:
                assert close_handle(handle)

    try:
        with monkeypatch.context() as hooks:
            hooks.setattr(engine.tempfile, "TemporaryDirectory", LockedFileDirectory)
            assert engine._database_safety_code(database) == expected_code
        assert errors and all(exc.winerror == 32 for exc in errors)
        assert (roots[0] / database.name).exists(), "the native handle must block deletion"
        assert (database.read_bytes(), database.stat().st_mtime_ns) == before
        assert not database.with_name(f"{database.name}-shm").exists()
    finally:
        _remove_owned_snapshots(roots, temporary_root)
