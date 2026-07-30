"""Map model-visible workspace aliases to the configured host workspace."""

from __future__ import annotations

import os
from pathlib import Path, PurePath

_WORKSPACE_SEGMENT = "workspace"


def _is_rooted_path(path: PurePath) -> bool:
    return path.is_absolute() or bool(path.root)


def _canonical_if_concrete(path: PurePath) -> PurePath:
    if isinstance(path, Path):
        try:
            return path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            # Callers still validate the original lexical spelling. Keep this
            # helper total so malformed paths and symlink loops fail at the
            # backend's typed validation boundary instead of leaking a raw
            # pathlib exception.
            return path
    return path


def _lexical_if_concrete(path: PurePath) -> PurePath:
    if isinstance(path, Path):
        return Path(os.path.abspath(os.fspath(path.expanduser())))
    return path


def resolve_workspace_alias[PathT: PurePath](
    raw_path: PurePath,
    workspace_root: PathT | None,
) -> PathT | None:
    """Map an absolute ``.../workspace/...`` spelling to ``workspace_root``.

    The tail after the final literal ``workspace`` segment is preserved.
    Relative paths and paths already resolving inside the active workspace are
    not aliases.  Concrete host paths are resolved with ``strict=False`` so a
    real symlink into the workspace keeps its original logical view.  A mapped
    result remains lexical so symlinks in the workspace-relative tail do not
    erase protected metadata or other carveouts before policy evaluation.
    """

    if workspace_root is None or not _is_rooted_path(raw_path):
        return None

    for candidate, root in (
        (
            _lexical_if_concrete(raw_path),
            _lexical_if_concrete(workspace_root),
        ),
        (
            _canonical_if_concrete(raw_path),
            _canonical_if_concrete(workspace_root),
        ),
    ):
        try:
            candidate.relative_to(root)
        except (TypeError, ValueError):
            continue
        return None

    last_workspace_index = -1
    for index, segment in enumerate(raw_path.parts):
        if segment == _WORKSPACE_SEGMENT:
            last_workspace_index = index
    if last_workspace_index < 0:
        return None

    tail_parts = raw_path.parts[last_workspace_index + 1 :]
    return workspace_root.joinpath(*tail_parts)


__all__ = ["resolve_workspace_alias"]
