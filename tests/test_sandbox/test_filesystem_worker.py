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
from opensquilla.sandbox.permissions import FileSystemAccess


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


def test_filesystem_profile_parses_optional_logical_path(tmp_path: Path) -> None:
    target = tmp_path / "metadata-target"
    logical = tmp_path / "workspace" / ".git"
    payload = {
        "permissions": {
            "filesystem": {
                "profile": {
                    "entries": [
                        {
                            "path": str(target),
                            "access": "read",
                            "logicalPath": str(logical),
                        }
                    ],
                    "deniedReadGlobs": [],
                    "defaultAccess": "deny",
                }
            }
        }
    }

    profile = filesystem_worker._filesystem_profile(payload)

    assert profile is not None
    assert profile.entries[0].path == target
    assert profile.entries[0].logical_path == logical


@pytest.mark.parametrize(
    "entry",
    (
        {"path": "/metadata-target", "access": "read"},
        {"path": "/metadata-target", "access": "read", "logicalPath": None},
    ),
)
def test_filesystem_profile_accepts_legacy_missing_or_null_logical_path(
    entry: dict[str, object],
) -> None:
    payload = {
        "permissions": {
            "filesystem": {
                "profile": {
                    "entries": [entry],
                    "deniedReadGlobs": [],
                    "defaultAccess": "deny",
                }
            }
        }
    }

    profile = filesystem_worker._filesystem_profile(payload)

    assert profile is not None
    assert profile.entries[0].logical_path is None


@pytest.mark.parametrize("logical_path", ("", "relative/.git", 17))
def test_filesystem_profile_rejects_invalid_present_logical_path(
    logical_path: object,
) -> None:
    payload = {
        "permissions": {
            "filesystem": {
                "profile": {
                    "entries": [
                        {
                            "path": "/metadata-target",
                            "access": "read",
                            "logicalPath": logical_path,
                        }
                    ],
                    "deniedReadGlobs": [],
                    "defaultAccess": "deny",
                }
            }
        }
    }

    with pytest.raises(
        ValueError,
        match="logicalPath must be a non-empty absolute path",
    ):
        filesystem_worker._filesystem_profile(payload)


def test_enforce_candidate_access_checks_logical_before_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    target = tmp_path / "target"
    workspace.mkdir()
    target.mkdir()
    alias = workspace / "alias"
    _make_symlink(alias, target)
    logical = alias / "note.txt"
    canonical = target / "note.txt"
    canonical.write_text("before", encoding="utf-8")
    checked: list[Path] = []

    class _AllowingProfile:
        def resolve(self, path: Path) -> FileSystemAccess:
            checked.append(Path(path))
            return FileSystemAccess.WRITE

    profile = _AllowingProfile()
    monkeypatch.setattr(filesystem_worker, "_filesystem_profile", lambda _payload: profile)

    resolved = filesystem_worker._enforce_candidate_access(
        {},
        logical,
        write=True,
    )

    assert resolved == canonical
    assert checked == [logical, canonical]


def test_list_dir_keeps_siblings_when_symlink_target_is_missing(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("hello", encoding="utf-8")
    _make_symlink(tmp_path / "dangling", tmp_path / "missing-target")

    result = filesystem_worker._list_dir({"path": str(tmp_path), "displayPath": str(tmp_path)})

    assert "[file] ok.txt (5 bytes)" in result["message"]
    assert "[link] dangling (broken symlink)" in result["message"]


def test_list_dir_keeps_siblings_when_symlink_target_loops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "ok.txt").write_text("hello", encoding="utf-8")
    loop = tmp_path / "loop"
    loop.write_text("placeholder", encoding="utf-8")
    original_resolve = Path.resolve
    original_lstat = Path.lstat
    original_stat = Path.stat

    def selective_resolve(path: Path, *args: object, **kwargs: object):
        if path == loop:
            raise RuntimeError("Symlink loop")
        return original_resolve(path, *args, **kwargs)

    def selective_lstat(path: Path):
        if path == loop:
            metadata = original_lstat(path)
            values = list(metadata)
            values[stat.ST_MODE] = stat.S_IFLNK
            return os.stat_result(values)
        return original_lstat(path)

    def selective_stat(path: Path, *args: object, **kwargs: object):
        if path == loop and kwargs.get("follow_symlinks", True):
            raise AssertionError("unresolvable link target must not be followed")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", selective_resolve)
    monkeypatch.setattr(Path, "lstat", selective_lstat)
    monkeypatch.setattr(Path, "stat", selective_stat)

    result = filesystem_worker._list_dir({"path": str(tmp_path), "displayPath": str(tmp_path)})

    assert "[file] ok.txt (5 bytes)" in result["message"]
    assert "[link] loop (target metadata unavailable)" in result["message"]


def test_read_file_uses_verified_target_after_symlink_is_retargeted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = tmp_path / "allowed.txt"
    link = tmp_path / "link.txt"
    allowed.write_text("allowed", encoding="utf-8")
    link.write_text("secret", encoding="utf-8")

    def verified_target(payload, candidate, **kwargs):
        assert Path(candidate) == link
        return allowed

    monkeypatch.setattr(
        filesystem_worker,
        "_enforce_candidate_access",
        verified_target,
    )

    result = filesystem_worker._read_file({"path": str(link), "displayPath": str(link)})

    assert "allowed" in result["message"]
    assert "secret" not in result["message"]


def test_glob_keeps_logical_link_name_when_verified_target_is_outside_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    logical = base / "link.txt"
    logical.write_text("placeholder", encoding="utf-8")
    verified = tmp_path / "outside.txt"
    verified.write_text("allowed", encoding="utf-8")
    original_enforce = filesystem_worker._enforce_candidate_access

    def verified_target(payload, candidate, **kwargs):
        if Path(candidate) == logical:
            return verified
        return original_enforce(payload, candidate, **kwargs)

    monkeypatch.setattr(
        filesystem_worker,
        "_enforce_candidate_access",
        verified_target,
    )

    result = filesystem_worker._glob_search({"path": str(base), "pattern": "link.txt"})

    assert result["message"] == str(logical)


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


def test_grep_search_does_not_enter_explicitly_denied_subtree(tmp_path: Path) -> None:
    visible = tmp_path / "visible.txt"
    denied = tmp_path / "denied"
    denied.mkdir()
    secret = denied / "secret.txt"
    visible.write_text("needle visible", encoding="utf-8")
    secret.write_text("needle secret", encoding="utf-8")
    payload = {
        "kind": "grep_search",
        "path": str(tmp_path),
        "pattern": "needle",
        "permissions": {
            "filesystem": {
                "profile": {
                    "entries": [
                        {"path": str(tmp_path), "access": "read"},
                        {"path": str(denied), "access": "deny"},
                    ],
                    "deniedReadGlobs": [],
                    "defaultAccess": "deny",
                }
            }
        },
    }

    result = filesystem_worker._grep_search(payload)

    assert str(visible) in result["message"]
    assert str(secret) not in result["message"]
    assert "needle secret" not in result["message"]


def test_grep_search_only_enters_current_transcript_session(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    transcript_root = media_root / "transcripts"
    current = transcript_root / "session-current"
    other = transcript_root / "session-other"
    current.mkdir(parents=True)
    other.mkdir()
    current_file = current / "current.txt"
    other_file = other / "other.txt"
    current_file.write_text("needle current", encoding="utf-8")
    other_file.write_text("needle other", encoding="utf-8")
    payload = {
        "kind": "grep_search",
        "path": str(media_root),
        "pattern": "needle",
        "permissions": {
            "filesystem": {
                "profile": {
                    "entries": [{"path": str(media_root), "access": "read"}],
                    "deniedReadGlobs": [],
                    "defaultAccess": "deny",
                },
                "workspaceStrict": True,
                "transcriptBase": str(transcript_root),
                "transcriptSessionRoot": str(current),
            }
        },
    }

    result = filesystem_worker._grep_search(payload)

    assert str(current_file) in result["message"]
    assert str(other_file) not in result["message"]
    assert "needle other" not in result["message"]


def test_write_text_blocks_another_attachment_session(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    attachment_root = workspace / ".opensquilla" / "attachments"
    current = attachment_root / "session-current"
    other = attachment_root / "session-other"
    current.mkdir(parents=True)
    other.mkdir()
    target = other / "blocked.txt"
    payload = {
        "kind": "write_text",
        "path": str(target),
        "content": "must not be written",
        "permissions": {
            "filesystem": {
                "profile": {
                    "entries": [{"path": str(workspace), "access": "write"}],
                    "deniedReadGlobs": [],
                    "defaultAccess": "read",
                },
                "workspaceStrict": True,
                "attachmentBase": str(attachment_root),
                "attachmentSessionRoot": str(current),
            }
        },
    }

    with pytest.raises(PermissionError, match="another session's attachments"):
        filesystem_worker._write_text(payload)

    assert not target.exists()


def test_write_text_rejects_redirected_current_attachment_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    attachment_root = workspace / ".opensquilla" / "attachments"
    current = attachment_root / "session-current"
    other = attachment_root / "session-other"
    current.mkdir(parents=True)
    other.mkdir()
    target = other / "blocked.txt"
    original_resolve = Path.resolve

    def redirected_session_root(path: Path, *args: object, **kwargs: object):
        if path == current:
            return other
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected_session_root)
    payload = {
        "kind": "write_text",
        "path": str(target),
        "content": "must not be written",
        "permissions": {
            "filesystem": {
                "profile": {
                    "entries": [{"path": str(workspace), "access": "write"}],
                    "deniedReadGlobs": [],
                    "defaultAccess": "read",
                },
                "workspaceStrict": True,
                "attachmentBase": str(attachment_root),
                "attachmentSessionRoot": str(current),
            }
        },
    }

    with pytest.raises(PermissionError, match="must not be a redirected path"):
        filesystem_worker._write_text(payload)

    assert not target.exists()


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


def test_apply_patch_gates_original_logical_path_before_authorized_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    target_root = tmp_path / "target"
    workspace.mkdir()
    target_root.mkdir()
    alias = workspace / "alias"
    _make_symlink(alias, target_root)
    target = target_root / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    checked: list[Path] = []

    def record_access(
        _payload: dict[str, object],
        candidate: Path,
        **_kwargs: object,
    ) -> Path:
        checked.append(Path(candidate))
        return Path(candidate).resolve(strict=False)

    monkeypatch.setattr(
        filesystem_worker,
        "_enforce_candidate_access",
        record_access,
    )

    result = filesystem_worker._run(
        {
            "kind": "apply_patch",
            "root": str(workspace),
            "paths": [str(target)],
            "patch": """*** Begin Patch
*** Update File: alias/note.txt
@@ -1,1 +1,1 @@
-before
+after
*** End Patch""",
        }
    )

    assert result == {"message": "Applied patch: 1 file(s) modified", "created": False}
    assert checked == [alias / "note.txt", target]
    assert target.read_text(encoding="utf-8") == "after\n"


def test_apply_patch_rejects_target_missing_from_explicit_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authorized = tmp_path / "outside" / "authorized.txt"
    unauthorized = tmp_path / "outside" / "unauthorized.txt"
    authorized.parent.mkdir()
    unauthorized.write_text("before\n", encoding="utf-8")

    with pytest.raises(ValueError, match="authorization changed after validation"):
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


def test_apply_patch_rejects_changed_target_inside_patch_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authorized = workspace / "authorized.txt"
    changed = workspace / "changed.txt"
    authorized.write_text("before\n", encoding="utf-8")
    changed.write_text("before\n", encoding="utf-8")

    with pytest.raises(ValueError, match="authorization changed after validation"):
        filesystem_worker._run(
            {
                "kind": "apply_patch",
                "root": str(workspace),
                "paths": [str(authorized)],
                "patch": """*** Begin Patch
*** Update File: changed.txt
@@ -1,1 +1,1 @@
-before
+after
*** End Patch""",
            }
        )

    assert changed.read_text(encoding="utf-8") == "before\n"
