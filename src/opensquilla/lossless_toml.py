"""Conservative, comment-preserving TOML patches for complete profile import."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from typing import Any

import tomli_w

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


class LosslessTomlPatchError(ValueError):
    """The requested semantic change cannot be expressed without a rewrite."""


@dataclass(frozen=True)
class _Assignment:
    path: tuple[str | int, ...]
    line_index: int
    equals_index: int
    value_start: int
    value_end: int
    comment_start: int | None
    newline: str
    indent: str


def _key_path(expression: str) -> tuple[str, ...]:
    try:
        payload: object = tomllib.loads(f"{expression} = 0")
    except tomllib.TOMLDecodeError as exc:
        raise LosslessTomlPatchError(f"unsupported TOML key expression: {expression}") from exc
    parts: list[str] = []
    while isinstance(payload, dict) and len(payload) == 1:
        key, payload = next(iter(payload.items()))
        parts.append(str(key))
    if payload != 0 or not parts:
        raise LosslessTomlPatchError(f"ambiguous TOML key expression: {expression}")
    return tuple(parts)


def _comment_start(text: str) -> int | None:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(text):
        if quote == '"':
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if quote == "'":
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "#":
            return index
    return None


def _assignment_equals(line: str) -> int | None:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(line):
        if quote == '"':
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if quote == "'":
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "#":
            return None
        elif character == "=":
            return index
    return None


def _split_newline(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    return line, ""


def _scan(
    lines: list[str],
) -> tuple[
    dict[tuple[str | int, ...], _Assignment],
    dict[tuple[str | int, ...], int],
]:
    assignments: dict[tuple[str | int, ...], _Assignment] = {}
    insertion_points: dict[tuple[str | int, ...], int] = {(): len(lines)}
    current: tuple[str | int, ...] = ()
    array_counts: dict[tuple[str, ...], int] = {}
    first_header = len(lines)

    for index, raw_line in enumerate(lines):
        line, newline = _split_newline(raw_line)
        stripped = line.strip()
        if stripped.startswith("["):
            comment = _comment_start(stripped)
            header = stripped if comment is None else stripped[:comment].rstrip()
            is_array = header.startswith("[[") and header[-2:] == "]]"
            is_table = header.startswith("[") and header.endswith("]")
            if not is_array and not is_table:
                raise LosslessTomlPatchError("unsupported or multiline TOML table header")
            inner = header[2:-2] if is_array else header[1:-1]
            table = _key_path(inner.strip())
            if is_array:
                occurrence = array_counts.get(table, 0)
                array_counts[table] = occurrence + 1
                current = (*table, occurrence)
            else:
                current = table
            insertion_points[current] = index + 1
            first_header = min(first_header, index)
            continue

        equals = _assignment_equals(line)
        if equals is None:
            continue
        key_expression = line[:equals].strip()
        if not key_expression:
            raise LosslessTomlPatchError("empty TOML assignment key")
        path = (*current, *_key_path(key_expression))
        if path in assignments:
            raise LosslessTomlPatchError(f"duplicate semantic TOML assignment: {path}")
        suffix = line[equals + 1 :]
        leading = len(suffix) - len(suffix.lstrip())
        comment_relative = _comment_start(suffix)
        value_region = suffix if comment_relative is None else suffix[:comment_relative]
        value_end_relative = len(value_region.rstrip())
        if not value_region.strip():
            raise LosslessTomlPatchError(f"empty or multiline TOML value: {path}")
        indent = line[: len(line) - len(line.lstrip())]
        assignments[path] = _Assignment(
            path=path,
            line_index=index,
            equals_index=equals,
            value_start=equals + 1 + leading,
            value_end=equals + 1 + value_end_relative,
            comment_start=(
                equals + 1 + comment_relative if comment_relative is not None else None
            ),
            newline=newline,
            indent=indent,
        )
        insertion_points[current] = index + 1

    insertion_points[()] = min(insertion_points.get((), first_header), first_header)
    return assignments, insertion_points


def _leaves(value: object, path: tuple[str | int, ...] = ()) -> dict[tuple[str | int, ...], Any]:
    if isinstance(value, dict):
        result: dict[tuple[str | int, ...], Any] = {}
        for key, child in value.items():
            result.update(_leaves(child, (*path, str(key))))
        return result
    if isinstance(value, list):
        result = {}
        for index, child in enumerate(value):
            result.update(_leaves(child, (*path, index)))
        return result
    return {path: value}


def _toml_scalar(value: object) -> str:
    try:
        rendered = tomli_w.dumps({"value": value}).strip()
    except (TypeError, ValueError) as exc:
        raise LosslessTomlPatchError("unsupported TOML replacement value") from exc
    prefix = "value = "
    if not rendered.startswith(prefix) or "\n" in rendered:
        raise LosslessTomlPatchError("replacement value requires a multiline TOML rewrite")
    return rendered[len(prefix) :]


def _render_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def patch_import_config(
    raw: bytes,
    original: dict[str, Any],
    transformed: dict[str, Any],
) -> bytes:
    """Patch only changed leaf assignments and prove the exact final payload."""

    try:
        text = raw.decode("utf-8")
        if tomllib.loads(text) != original:
            raise LosslessTomlPatchError("source TOML bytes no longer match validated config")
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise LosslessTomlPatchError("source config is not valid UTF-8 TOML") from exc
    if original == transformed:
        return raw

    lines = text.splitlines(keepends=True)
    assignments, insertion_points = _scan(lines)
    original_leaves = _leaves(original)
    transformed_leaves = _leaves(transformed)
    removed = set(original_leaves) - set(transformed_leaves)
    added = set(transformed_leaves) - set(original_leaves)
    changed = {
        path
        for path in set(original_leaves) & set(transformed_leaves)
        if original_leaves[path] != transformed_leaves[path]
    }
    replacements: dict[int, str] = {}
    for path in sorted(removed, key=repr):
        assignment = assignments.get(path)
        if assignment is None:
            raise LosslessTomlPatchError(f"cannot remove non-scalar TOML path losslessly: {path}")
        line, _newline = _split_newline(lines[assignment.line_index])
        comment = line[assignment.comment_start :] if assignment.comment_start is not None else ""
        replacements[assignment.line_index] = (
            f"{assignment.indent}{comment}{assignment.newline}" if comment else ""
        )

    for path in sorted(changed, key=repr):
        assignment = assignments.get(path)
        if assignment is None:
            raise LosslessTomlPatchError(f"cannot replace non-scalar TOML path: {path}")
        line, _newline = _split_newline(lines[assignment.line_index])
        replacements[assignment.line_index] = (
            line[: assignment.value_start]
            + _toml_scalar(transformed_leaves[path])
            + line[assignment.value_end :]
            + assignment.newline
        )

    insertions: dict[int, list[str]] = {}
    newline = "\r\n" if "\r\n" in text else "\n"
    contexts = tuple(insertion_points)
    for path in sorted(added, key=repr):
        compatible = [
            context
            for context in contexts
            if len(context) < len(path) and path[: len(context)] == context
        ]
        if not compatible:
            raise LosslessTomlPatchError(f"no existing TOML table can contain: {path}")
        context = max(compatible, key=len)
        remainder = path[len(context) :]
        if not remainder or any(not isinstance(part, str) for part in remainder):
            raise LosslessTomlPatchError(f"array-table insertion is not lossless: {path}")
        expression = ".".join(_render_key(str(part)) for part in remainder)
        insertion = f"{expression} = {_toml_scalar(transformed_leaves[path])}{newline}"
        insertions.setdefault(insertion_points[context], []).append(insertion)

    output: list[str] = []
    for index in range(len(lines) + 1):
        output.extend(insertions.get(index, ()))
        if index < len(lines):
            output.append(replacements.get(index, lines[index]))
    patched = "".join(output)
    try:
        parsed = tomllib.loads(patched)
    except tomllib.TOMLDecodeError as exc:
        raise LosslessTomlPatchError("lossless patch produced invalid TOML") from exc
    if parsed != transformed:
        raise LosslessTomlPatchError("lossless patch could not prove the transformed payload")
    return patched.encode("utf-8")


__all__ = ["LosslessTomlPatchError", "patch_import_config"]
