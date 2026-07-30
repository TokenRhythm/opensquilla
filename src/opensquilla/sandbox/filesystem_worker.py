"""Restricted filesystem side-effect worker."""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import json
import re
import stat
import sys
from collections.abc import Sequence
from pathlib import Path, PurePath
from typing import Any

from opensquilla.sandbox.directory_listing import format_directory_entry
from opensquilla.sandbox.path_aliases import resolve_workspace_alias
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionEntry,
    FileSystemPermissionProfile,
    logical_absolute_path,
)


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) != 1:
            raise ValueError("filesystem worker expects one payload source")
        payload = _load_payload(args[0])
        result = _run(payload)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "type": type(exc).__name__,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


def _load_payload(source: str | Path) -> dict[str, Any]:
    raw_payload = (
        sys.stdin.read() if str(source) == "-" else Path(source).read_text(encoding="utf-8")
    )
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError("filesystem worker payload must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("filesystem worker payload must be an object")
    return payload


def _run(payload: dict[str, Any]) -> dict[str, object]:
    kind = payload.get("kind")
    if kind == "read_file":
        return _read_file(payload)
    if kind == "list_dir":
        return _list_dir(payload)
    if kind == "glob_search":
        return _glob_search(payload)
    if kind == "grep_search":
        return _grep_search(payload)
    if kind == "write_text":
        return _write_text(payload)
    if kind == "edit_text":
        return _edit_text(payload)
    if kind == "create_source":
        return _create_source(payload)
    if kind == "edit_source":
        return _edit_source(payload)
    if kind == "apply_patch":
        return _apply_patch(payload)
    raise ValueError(f"unsupported filesystem operation: {kind!r}")


def _required_path(payload: dict[str, Any], key: str) -> Path:
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"filesystem operation missing {key}")
    return Path(raw)


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"filesystem operation missing {key}")
    return value


def _required_paths(payload: dict[str, Any], key: str) -> tuple[Path, ...]:
    values = payload.get(key)
    if not isinstance(values, list) or not values:
        raise ValueError(f"filesystem operation missing {key}")
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError(f"filesystem operation {key} must contain paths")
    return tuple(Path(value).resolve(strict=False) for value in values)


def _required_edits(payload: dict[str, Any]) -> list[dict[str, object]]:
    edits = payload.get("edits")
    if not isinstance(edits, list) or not all(isinstance(edit, dict) for edit in edits):
        raise ValueError("filesystem operation edits must be an array of objects")
    return edits


def _optional_positive_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"filesystem operation {key} must be an integer")
    return value if value > 0 else None


def _filesystem_boundary(payload: dict[str, Any]) -> dict[str, Any]:
    permissions = payload.get("permissions")
    if not isinstance(permissions, dict):
        return {}
    filesystem = permissions.get("filesystem")
    return filesystem if isinstance(filesystem, dict) else {}


def _filesystem_profile(payload: dict[str, Any]) -> FileSystemPermissionProfile | None:
    cached = payload.get("_filesystemProfileCache")
    if isinstance(cached, FileSystemPermissionProfile):
        return cached
    raw = _filesystem_boundary(payload).get("profile")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("filesystem worker profile must be an object")
    raw_entries = raw.get("entries")
    raw_globs = raw.get("deniedReadGlobs", [])
    raw_default = raw.get("defaultAccess")
    if (
        not isinstance(raw_entries, list)
        or not isinstance(raw_globs, list)
        or not all(isinstance(pattern, str) for pattern in raw_globs)
        or not isinstance(raw_default, str)
    ):
        raise ValueError("filesystem worker profile is invalid")
    entries: list[FileSystemPermissionEntry] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            raise ValueError("filesystem worker profile entry must be an object")
        path = item.get("path")
        access = item.get("access")
        if not isinstance(path, str) or not isinstance(access, str):
            raise ValueError("filesystem worker profile entry is invalid")
        raw_logical_path = item.get("logicalPath")
        logical_path: Path | None = None
        if raw_logical_path is not None:
            if not isinstance(raw_logical_path, str) or not raw_logical_path:
                raise ValueError(
                    "filesystem worker profile logicalPath must be a non-empty absolute path"
                )
            logical_path = Path(raw_logical_path)
            if not logical_path.is_absolute():
                raise ValueError(
                    "filesystem worker profile logicalPath must be a non-empty absolute path"
                )
        entries.append(
            FileSystemPermissionEntry(
                Path(path),
                FileSystemAccess(access),
                logical_path=logical_path,
            )
        )
    profile = FileSystemPermissionProfile(
        entries=tuple(entries),
        denied_read_globs=tuple(raw_globs),
        default_access=FileSystemAccess(raw_default),
    )
    payload["_filesystemProfileCache"] = profile
    return profile


def _is_relative_to(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _session_boundary_root(
    base_raw: str,
    session_raw: str,
    *,
    label: str,
) -> tuple[Path, Path]:
    """Return stable base/session roots, rejecting a redirected session root."""

    try:
        lexical_base = Path(base_raw).expanduser().absolute()
        resolved_base = lexical_base.resolve(strict=False)
        lexical_session = Path(session_raw).expanduser().absolute()
        resolved_session = lexical_session.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PermissionError(f"{label} session boundary cannot be resolved safely") from exc
    try:
        relative_session = lexical_session.relative_to(lexical_base)
    except ValueError as exc:
        raise PermissionError(f"{label} session root is outside its boundary") from exc
    expected_session = resolved_base / relative_session
    if str(expected_session).casefold() != str(resolved_session).casefold():
        raise PermissionError(f"{label} session root must not be a redirected path")
    return resolved_base, resolved_session


def _enforce_candidate_access(
    payload: dict[str, Any],
    candidate: Path,
    *,
    write: bool = False,
    traversal: bool = False,
    metadata_only: bool = False,
) -> Path:
    """Resolve and preflight one path inside an already-confined worker.

    The platform backend remains the security boundary (Windows restricted
    token plus ACLs, bwrap, or Seatbelt).  This check mirrors that policy for
    useful errors and prunes workspace-strict session data before access.
    """
    logical = Path(logical_absolute_path(candidate))
    profile = _filesystem_profile(payload)
    if profile is not None:
        _enforce_profile_access(profile, logical, write=write)
    try:
        resolved = candidate.expanduser().resolve(strict=False)
        profile_candidate: PurePath = resolved
    except (OSError, RuntimeError) as exc:
        lexical = candidate.expanduser().absolute()
        try:
            is_link = stat.S_ISLNK(lexical.lstat().st_mode)
        except OSError:
            is_link = False
        if not metadata_only or not is_link:
            raise PermissionError(f"cannot safely resolve filesystem path: {candidate}") from exc
        # Listing a directory only needs the link's own metadata. Keep its
        # lexical name so an unresolvable target cannot fail the whole list.
        resolved = lexical
        profile_candidate = PurePath(str(lexical))
    if profile is not None:
        _enforce_profile_access(profile, profile_candidate, write=write)
    boundary = _filesystem_boundary(payload)
    if not boundary.get("workspaceStrict"):
        return resolved

    attachment_base_raw = boundary.get("attachmentBase")
    attachment_session_raw = boundary.get("attachmentSessionRoot")
    if isinstance(attachment_base_raw, str) and attachment_base_raw:
        attachment_base = Path(attachment_base_raw).resolve(strict=False)
        if _is_relative_to(resolved, attachment_base):
            if resolved == attachment_base:
                return resolved
            if not isinstance(attachment_session_raw, str) or not attachment_session_raw:
                raise PermissionError(
                    f"filesystem worker blocks another session's attachments: {resolved}"
                )
            attachment_base, attachment_session = _session_boundary_root(
                attachment_base_raw,
                attachment_session_raw,
                label="attachment",
            )
            if not _is_relative_to(resolved, attachment_base) or not _is_relative_to(
                resolved,
                attachment_session,
            ):
                raise PermissionError(
                    f"filesystem worker blocks another session's attachments: {resolved}"
                )

    transcript_base_raw = boundary.get("transcriptBase")
    transcript_session_raw = boundary.get("transcriptSessionRoot")
    if isinstance(transcript_base_raw, str) and transcript_base_raw:
        transcript_base = Path(transcript_base_raw).resolve(strict=False)
        if _is_relative_to(resolved, transcript_base):
            if traversal and resolved == transcript_base:
                return resolved
            if not isinstance(transcript_session_raw, str) or not transcript_session_raw:
                raise PermissionError(
                    f"filesystem worker blocks another session's transcript: {resolved}"
                )
            transcript_base, transcript_session = _session_boundary_root(
                transcript_base_raw,
                transcript_session_raw,
                label="transcript",
            )
            if not _is_relative_to(resolved, transcript_base) or not _is_relative_to(
                resolved,
                transcript_session,
            ):
                raise PermissionError(
                    f"filesystem worker blocks another session's transcript: {resolved}"
                )
    return resolved


def _is_symlink_loop_resolution_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, RuntimeError) and str(current).startswith(
            "Symlink loop from "
        ):
            return True
        if isinstance(current, OSError) and current.errno == errno.ELOOP:
            return True
        current = current.__cause__ or current.__context__
    return False


def _enforce_profile_access(
    profile: FileSystemPermissionProfile,
    candidate: PurePath,
    *,
    write: bool,
) -> None:
    access = profile.resolve(candidate)
    if access is FileSystemAccess.DENY or (write and access is not FileSystemAccess.WRITE):
        raise PermissionError(f"filesystem profile denies access to {candidate}")


def _safe_descendants(
    payload: dict[str, Any],
    logical_base: Path,
    verified_base: Path,
):
    pending = [(logical_base, verified_base)]
    visited: set[str] = set()
    while pending:
        logical_directory, verified_directory = pending.pop()
        canonical = str(verified_directory).casefold()
        if canonical in visited:
            continue
        visited.add(canonical)
        try:
            children = sorted(verified_directory.iterdir(), key=lambda item: str(item))
        except (PermissionError, OSError):
            continue
        for child in children:
            try:
                checked = _enforce_candidate_access(payload, child, traversal=True)
            except PermissionError:
                continue
            logical_child = logical_directory / child.name
            yield logical_child, checked
            try:
                if checked.is_dir():
                    pending.append((logical_child, checked))
            except (PermissionError, OSError):
                continue


def _readable_candidate(
    payload: dict[str, Any],
    candidate: Path,
) -> Path | None:
    try:
        return _enforce_candidate_access(payload, candidate, traversal=True)
    except PermissionError:
        return None


def _relative_glob_match(base: Path, candidate: Path, pattern: str) -> bool:
    relative = candidate.relative_to(base)
    normalized_pattern = pattern.replace("\\", "/")
    if "/" not in normalized_pattern and "**" not in normalized_pattern:
        return len(relative.parts) == 1 and fnmatch.fnmatch(relative.name, normalized_pattern)
    if relative.match(normalized_pattern):
        return True
    if normalized_pattern.startswith("**/"):
        return relative.match(normalized_pattern[3:])
    return False


def _read_file(payload: dict[str, Any]) -> dict[str, object]:
    from opensquilla.tools.builtin import filesystem as filesystem_tool

    path = _required_path(payload, "path")
    path = _enforce_candidate_access(payload, path)
    display_path = payload.get("displayPath") or str(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {display_path}")
    if not path.is_file():
        raise IsADirectoryError(f"Path is a directory: {display_path}")

    sample = filesystem_tool._read_binary_sample(path)
    if not sample:
        return {"message": ""}
    binary_reason = filesystem_tool._looks_binary(sample, path)
    if binary_reason:
        raise filesystem_tool._binary_file_error(
            str(display_path),
            path,
            reason=binary_reason,
        )
    return {
        "message": filesystem_tool._stream_numbered_lines_from_file(
            path,
            str(display_path),
            offset=_optional_positive_int(payload, "offset"),
            limit=_optional_positive_int(payload, "limit"),
        )
    }


def _list_dir(payload: dict[str, Any]) -> dict[str, object]:
    path = _required_path(payload, "path")
    path = _enforce_candidate_access(payload, path)
    display_path = payload.get("displayPath") or str(path)
    if not path.exists():
        raise FileNotFoundError(f"Path not found: {display_path}")
    if not path.is_dir():
        raise NotADirectoryError(f"Not a directory: {display_path}")

    dirs: list[str] = []
    files: list[str] = []
    for entry in sorted(path.iterdir(), key=lambda item: item.name):
        follow_target = True
        symlink_target_is_broken = False
        try:
            _enforce_candidate_access(payload, entry)
        except PermissionError as exc:
            symlink_target_is_broken = _is_symlink_loop_resolution_error(exc)
            try:
                _enforce_candidate_access(payload, entry, metadata_only=True)
            except PermissionError:
                continue
            follow_target = False
        is_directory, line = format_directory_entry(
            entry,
            follow_target=follow_target,
            symlink_target_is_broken=symlink_target_is_broken,
        )
        (dirs if is_directory else files).append(line)
    entries = dirs + files
    return {"message": "\n".join(entries) if entries else f"{display_path}: (empty directory)"}


def _glob_search(payload: dict[str, Any]) -> dict[str, object]:
    logical_base = _required_path(payload, "path")
    base = _enforce_candidate_access(payload, logical_base, traversal=True)
    pattern = _required_string(payload, "pattern")
    if not base.exists():
        raise FileNotFoundError(f"Path not found: {base}")
    recursive = "**" in pattern or "/" in pattern or "\\" in pattern
    candidates = (
        (
            logical
            for logical, _verified in _safe_descendants(
                payload,
                logical_base,
                base,
            )
        )
        if recursive
        else (
            logical_base / candidate.name
            for candidate in sorted(base.iterdir(), key=lambda item: str(item))
            if _readable_candidate(payload, candidate) is not None
        )
    )
    matches = [
        str(candidate)
        for candidate in candidates
        if _relative_glob_match(logical_base, candidate, pattern)
    ]
    return {
        "message": "\n".join(matches)
        if matches
        else f"No files matched pattern '{pattern}' in {base}"
    }


def _grep_search(payload: dict[str, Any]) -> dict[str, object]:
    base = _required_path(payload, "path")
    base = _enforce_candidate_access(payload, base, traversal=True)
    pattern = _required_string(payload, "pattern")
    include = payload.get("include")
    if include is not None and not isinstance(include, str):
        raise ValueError("filesystem operation include must be a string")
    max_results = _optional_positive_int(payload, "maxResults") or 100
    regex = re.compile(pattern)
    results: list[str] = []

    def search_file(path: Path) -> None:
        try:
            path = _enforce_candidate_access(payload, path)
        except PermissionError:
            return
        if include and not fnmatch.fnmatch(path.name, include):
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except (PermissionError, OSError):
            return
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                results.append(f"{path}:{lineno}: {line.rstrip()}")
                if len(results) >= max_results:
                    return

    if base.is_file():
        search_file(base)
    else:
        for _logical_path, path in _safe_descendants(payload, base, base):
            if len(results) >= max_results:
                break
            if path.is_file():
                search_file(path)

    return {
        "message": "\n".join(results)
        if results
        else f"No matches for pattern '{pattern}' in {base}"
    }


def _write_text(payload: dict[str, Any]) -> dict[str, object]:
    path = _required_path(payload, "path")
    path = _enforce_candidate_access(payload, path, write=True)
    content = _required_string(payload, "content")
    created = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {
        "message": f"Written {len(content)} bytes to {path}",
        "created": created,
    }


def _edit_text(payload: dict[str, Any]) -> dict[str, object]:
    path = _required_path(payload, "path")
    path = _enforce_candidate_access(payload, path, write=True)
    old_text = _required_string(payload, "oldText")
    new_text = _required_string(payload, "newText")
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    original = path.read_text(encoding="utf-8")
    if old_text not in original:
        raise ValueError(f"old_text not found in {path}")
    count = original.count(old_text)
    if count > 1:
        raise ValueError(f"old_text matches {count} locations in {path}; be more specific")
    path.write_text(original.replace(old_text, new_text, 1), encoding="utf-8")
    return {
        "message": f"Edited {path}: replaced {len(old_text)} chars with {len(new_text)} chars",
        "created": False,
    }


def _authorized_source_target(payload: dict[str, Any]) -> Path:
    authorized = Path(logical_absolute_path(_required_path(payload, "path")))
    raw_logical = Path(logical_absolute_path(_required_path(payload, "logicalPath")))
    workspace = _required_path(payload, "workspace")
    logical = resolve_workspace_alias(raw_logical, workspace) or raw_logical
    current = _enforce_candidate_access(payload, logical, write=True)
    authorized_current = _enforce_candidate_access(payload, authorized, write=True)
    if current != authorized or authorized_current != authorized:
        raise PermissionError(
            f"authorized filesystem target changed before source write: {raw_logical}"
        )
    return authorized


def _source_revision(data: bytes) -> str:
    return f"file_{hashlib.sha256(data).hexdigest()[:16]}"


def _source_fingerprint(data: bytes | None) -> dict[str, object]:
    if data is None:
        return {"exists": False, "size": 0, "sha256": None}
    return {
        "exists": True,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _create_source(payload: dict[str, Any]) -> dict[str, object]:
    path = _authorized_source_target(payload)
    content = _required_string(payload, "content")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(content)
    return {
        "message": f"Created source file {path}",
        "created": True,
        "status": "created",
        "afterRevision": _source_revision(content.encode("utf-8")),
        "beforeFingerprint": _source_fingerprint(None),
        "afterFingerprint": _source_fingerprint(content.encode("utf-8")),
    }


def _edit_source(payload: dict[str, Any]) -> dict[str, object]:
    from opensquilla.tools.source_edit_contract import (
        SourceEditContractError,
        apply_line_edits,
    )

    path = _authorized_source_target(payload)
    expected_revision = _required_string(payload, "expectedRevision")
    edits = _required_edits(payload)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not path.is_file():
        raise IsADirectoryError(f"Path is a directory: {path}")

    with path.open("r+b") as handle:
        original_bytes = handle.read()
        try:
            original = original_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"source file is not valid UTF-8: {path}") from exc
        before_revision = _source_revision(original_bytes)
        if before_revision != expected_revision:
            return {
                "message": "revision conflict",
                "status": "revision_conflict",
                "beforeRevision": before_revision,
            }
        try:
            updated = apply_line_edits(original, edits)
        except SourceEditContractError as exc:
            return {
                "message": str(exc),
                "status": "contract_error",
            }
        updated_bytes = updated.encode("utf-8")
        if updated_bytes != original_bytes:
            handle.seek(0)
            handle.write(updated_bytes)
            handle.truncate()
            handle.flush()
        return {
            "message": f"Edited source file {path}",
            "created": False,
            "status": "applied",
            "original": original,
            "updated": updated,
            "beforeRevision": before_revision,
            "afterRevision": _source_revision(updated_bytes),
            "beforeFingerprint": _source_fingerprint(original_bytes),
            "afterFingerprint": _source_fingerprint(updated_bytes),
        }


def _apply_patch(payload: dict[str, Any]) -> dict[str, object]:
    from opensquilla.tools.builtin import patch as patch_tool

    patch = _required_string(payload, "patch")
    root = _required_path(payload, "root")
    ops = patch_tool._parse_patch(patch)
    for op in ops:
        raw_path = Path(op.path).expanduser()
        logical_path = raw_path if raw_path.is_absolute() else root / raw_path
        _enforce_candidate_access(
            payload,
            Path(logical_absolute_path(logical_path)),
            write=True,
        )
    authorized_paths = tuple(
        _enforce_candidate_access(payload, path, write=True)
        for path in _required_paths(payload, "paths")
    )
    added, modified, deleted, _planned = patch_tool._apply_ops(
        ops,
        root,
        authorized_paths=authorized_paths,
    )
    return {
        "message": _patch_summary(added=added, modified=modified, deleted=deleted),
        "created": added > 0,
    }


def _patch_summary(*, added: int, modified: int, deleted: int) -> str:
    parts: list[str] = []
    if added:
        parts.append(f"{added} file(s) added")
    if modified:
        parts.append(f"{modified} file(s) modified")
    if deleted:
        parts.append(f"{deleted} file(s) deleted")
    summary = ", ".join(parts) if parts else "no changes"
    return f"Applied patch: {summary}"


if __name__ == "__main__":  # pragma: no cover
    main()
