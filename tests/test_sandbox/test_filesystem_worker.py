from __future__ import annotations

import errno
import json
import os
import stat
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.sandbox import directory_listing, filesystem_worker


def _make_symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unsupported/unavailable: {exc}")


def _windows_cannot_resolve_filename_error() -> OSError:
    error = OSError(errno.EINVAL, "cannot resolve filename")
    error.winerror = 1921  # type: ignore[attr-defined]
    return error


def test_load_payload_reads_json_object_from_stdin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = {"kind": "read_file", "path": "/workspace/notes.txt"}
    (tmp_path / "-").write_text('{"source": "path"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(filesystem_worker.sys, "stdin", StringIO(json.dumps(expected)))

    assert filesystem_worker._load_payload("-") == expected


@pytest.mark.parametrize(
    ("payload", "message"),
    (("{", "valid JSON"), ("[]", "must be an object")),
)
def test_load_payload_rejects_invalid_stdin_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    (tmp_path / "-").write_text('{"source": "path"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(filesystem_worker.sys, "stdin", StringIO(payload))

    with pytest.raises(ValueError, match=message):
        filesystem_worker._load_payload("-")


def test_load_payload_retains_path_compatibility(tmp_path: Path) -> None:
    payload_path = tmp_path / "payload.json"
    expected = {"kind": "list_dir", "path": "/workspace"}
    payload_path.write_text(json.dumps(expected), encoding="utf-8")

    assert filesystem_worker._load_payload(payload_path) == expected


def test_list_dir_keeps_siblings_when_symlink_target_is_missing(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("hello", encoding="utf-8")
    _make_symlink(tmp_path / "dangling", tmp_path / "missing-target")

    result = filesystem_worker._list_dir(
        {"path": str(tmp_path), "displayPath": str(tmp_path)}
    )

    assert "[file] ok.txt (5 bytes)" in result["message"]
    assert "[link] dangling (broken symlink)" in result["message"]


def test_list_dir_keeps_siblings_when_regular_file_size_stat_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "ok.txt").write_text("hello", encoding="utf-8")
    blocked = tmp_path / "blocked.txt"
    blocked.write_text("secret", encoding="utf-8")
    original_stat = Path.stat

    def selective_stat(path: Path, *args: object, **kwargs: object):
        if path == blocked and kwargs.get("follow_symlinks", True):
            raise PermissionError("blocked for test")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", selective_stat)

    result = filesystem_worker._list_dir({"path": str(tmp_path)})

    assert "ok.txt" in result["message"]
    assert "[file] blocked.txt (size unavailable)" in result["message"]


def test_list_dir_keeps_siblings_when_child_metadata_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "ok.txt").write_text("hello", encoding="utf-8")
    blocked = tmp_path / "blocked.txt"
    blocked.write_text("secret", encoding="utf-8")
    original_lstat = Path.lstat

    def selective_lstat(path: Path):
        if path == blocked:
            raise PermissionError("blocked for test")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", selective_lstat)

    result = filesystem_worker._list_dir({"path": str(tmp_path)})

    assert "[file] ok.txt (5 bytes)" in result["message"]
    assert "[file] blocked.txt (metadata unavailable)" in result["message"]


def test_list_dir_distinguishes_unreadable_symlink_target_from_broken_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "ok.txt").write_text("hello", encoding="utf-8")
    target = tmp_path / "target.txt"
    target.write_text("secret", encoding="utf-8")
    link = tmp_path / "protected-link"
    _make_symlink(link, target)
    original_stat = Path.stat

    def selective_stat(path: Path, *args: object, **kwargs: object):
        if path == link and kwargs.get("follow_symlinks", True):
            raise PermissionError("target blocked for test")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", selective_stat)

    result = filesystem_worker._list_dir({"path": str(tmp_path)})

    assert "[file] ok.txt (5 bytes)" in result["message"]
    assert "[link] protected-link (target metadata unavailable)" in result["message"]
    assert "[link] protected-link (broken symlink)" not in result["message"]


@pytest.mark.parametrize(
    ("mode", "target_error", "expected"),
    (
        (
            stat.S_IFLNK,
            _windows_cannot_resolve_filename_error(),
            "[link] loop (broken symlink)",
        ),
        (
            stat.S_IFLNK,
            OSError(errno.EINVAL, "ordinary invalid argument"),
            "[link] loop (target metadata unavailable)",
        ),
        (
            stat.S_IFLNK,
            PermissionError(errno.EACCES, "target denied"),
            "[link] loop (target metadata unavailable)",
        ),
        (
            stat.S_IFREG,
            _windows_cannot_resolve_filename_error(),
            "[file] loop (size unavailable)",
        ),
    ),
)
def test_directory_entry_classifies_target_errors_without_native_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
    target_error: OSError,
    expected: str,
) -> None:
    entry = tmp_path / "loop"

    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _path: SimpleNamespace(st_mode=mode),
    )

    def fail_target_stat(_path: Path, *args: object, **kwargs: object):
        raise target_error

    monkeypatch.setattr(Path, "stat", fail_target_stat)

    assert directory_listing.format_directory_entry(entry) == (False, expected)


def test_list_dir_preserves_requested_directory_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_iterdir = Path.iterdir

    def selective_iterdir(path: Path):
        if path == tmp_path:
            raise PermissionError("directory denied for test")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", selective_iterdir)

    with pytest.raises(PermissionError, match="directory denied for test"):
        filesystem_worker._list_dir({"path": str(tmp_path)})


def test_apply_patch_accepts_explicit_target_outside_patch_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside" / "target.txt"
    target.parent.mkdir()
    target.write_text("before\n", encoding="utf-8")

    result = filesystem_worker._run(
        {
            "kind": "apply_patch",
            "root": str(workspace),
            "paths": [str(target)],
            "patch": f"""*** Begin Patch
*** Update File: {target.as_posix()}
@@ -1,1 +1,1 @@
-before
+after
*** End Patch""",
        }
    )

    assert result == {"message": "Applied patch: 1 file(s) modified", "created": False}
    assert target.read_text(encoding="utf-8") == "after\n"


def test_apply_patch_rejects_target_missing_from_explicit_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authorized = tmp_path / "outside" / "authorized.txt"
    unauthorized = tmp_path / "outside" / "unauthorized.txt"
    authorized.parent.mkdir()
    unauthorized.write_text("before\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outside patch root"):
        filesystem_worker._run(
            {
                "kind": "apply_patch",
                "root": str(workspace),
                "paths": [str(authorized)],
                "patch": f"""*** Begin Patch
*** Update File: {unauthorized.as_posix()}
@@ -1,1 +1,1 @@
-before
+after
*** End Patch""",
            }
        )

    assert unauthorized.read_text(encoding="utf-8") == "before\n"
