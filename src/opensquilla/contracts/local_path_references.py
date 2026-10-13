"""Explicit, display-only local path references; never file access authority."""

from __future__ import annotations

import re

_ABSOLUTE_PATH = re.compile(r"^(?:/|[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def normalize_local_path_references(value: object, *, message: str) -> tuple[str, ...]:
    """Accept only explicit paths matching the message's appended path suffix.

    This validates syntax, not existence, ownership, or readability. Keeping the
    original message intact means clients without this optional metadata retain
    the full input and tools still apply their existing file access checks.
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("localPathReferences must be an array")
    if not value:
        return ()
    if not isinstance(message, str) or len(message) > 100_000:
        raise ValueError("localPathReferences require a bounded message")
    paths: list[str] = []
    total = 0
    for path in value:
        if (
            not isinstance(path, str) or not path or path != path.strip()
            or len(path) > 32_768 or _CONTROL.search(path)
            or not _ABSOLUTE_PATH.match(path)
        ):
            raise ValueError("localPathReferences require bounded absolute paths without controls")
        total += len(path) + bool(paths)
        if total > 100_000:
            raise ValueError("localPathReferences exceed the message limit")
        paths.append(path)
    suffix = "\n".join(paths)
    if message != suffix and not message.endswith("\n" + suffix):
        raise ValueError("localPathReferences must match the appended message suffix")
    return tuple(paths)
