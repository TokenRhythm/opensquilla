"""Restricted filesystem side-effect worker."""

from __future__ import annotations

import fnmatch
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from opensquilla.sandbox.directory_listing import format_directory_entry


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
        sys.stdin.read()
        if str(source) == "-"
        else Path(source).read_text(encoding="utf-8")
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


def _optional_positive_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"filesystem operation {key} must be an integer")
    return value if value > 0 else None


def _read_file(payload: dict[str, Any]) -> dict[str, object]:
    from opensquilla.tools.builtin import filesystem as filesystem_tool

    path = _required_path(payload, "path")
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
    display_path = payload.get("displayPath") or str(path)
    if not path.exists():
        raise FileNotFoundError(f"Path not found: {display_path}")
    if not path.is_dir():
        raise NotADirectoryError(f"Not a directory: {display_path}")

    dirs: list[str] = []
    files: list[str] = []
    for entry in sorted(path.iterdir(), key=lambda item: item.name):
        is_directory, line = format_directory_entry(entry)
        (dirs if is_directory else files).append(line)
    entries = dirs + files
    return {"message": "\n".join(entries) if entries else f"{display_path}: (empty directory)"}


def _glob_search(payload: dict[str, Any]) -> dict[str, object]:
    base = _required_path(payload, "path")
    pattern = _required_string(payload, "pattern")
    if not base.exists():
        raise FileNotFoundError(f"Path not found: {base}")
    matches = [
        str(candidate)
        for candidate in sorted(base.glob(pattern), key=lambda item: str(item))
    ]
    return {
        "message": "\n".join(matches)
        if matches
        else f"No files matched pattern '{pattern}' in {base}"
    }


def _grep_search(payload: dict[str, Any]) -> dict[str, object]:
    base = _required_path(payload, "path")
    pattern = _required_string(payload, "pattern")
    include = payload.get("include")
    if include is not None and not isinstance(include, str):
        raise ValueError("filesystem operation include must be a string")
    max_results = _optional_positive_int(payload, "maxResults") or 100
    regex = re.compile(pattern)
    results: list[str] = []

    def search_file(path: Path) -> None:
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
        for path in base.rglob("*"):
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


def _apply_patch(payload: dict[str, Any]) -> dict[str, object]:
    from opensquilla.tools.builtin import patch as patch_tool

    patch = _required_string(payload, "patch")
    root = _required_path(payload, "root")
    authorized_paths = _required_paths(payload, "paths")
    ops = patch_tool._parse_patch(patch)
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
