"""Safety primitives for the uninstaller — path containment and protected-root guards.

Every destructive operation routes through here. The invariants this module
enforces (so the rest of the uninstaller can stay simple):

* **Containment** — a target is only deletable when, after symlink resolution, it
  is at or under a resolved root. :func:`is_within` mirrors the proven idiom in
  ``skills/hub/installer._is_relative_to`` / ``skills/loader``.
* **Protected roots** — the OpenSquilla "home" is whatever ``OPENSQUILLA_STATE_DIR``
  (or a relocated ``config.state_dir``) points at, with *zero* validation in
  ``paths.py``. A misconfiguration could aim it at ``$HOME``, ``/``, or
  ``~/Documents``. :func:`protected_root_reason` refuses to treat such paths as a
  blanket-deletion root, so a relocation can never escalate ``--purge-all`` into
  wiping unrelated user data.
"""

from __future__ import annotations

import os
from pathlib import Path

# Phrase the user must type verbatim to confirm a total data wipe (interactive).
PURGE_ALL_CONFIRM_PHRASE = "delete everything"

# A deletion root must have at least this many components below the filesystem
# anchor; refuses "/", "/usr", "/home", drive roots, etc.
_MIN_ROOT_DEPTH = 2

# Directory names whose immediate children are mount points / volume roots, not
# single-app homes (so a relocated home directly under one is refused).
_MOUNT_CONTAINER_NAMES = ("Volumes", "mnt", "media", "srv")

# The directory names an OpenSquilla home is expected to use. A blanket rmtree of
# a *root* is only performed when its leaf name is one of these (the positive
# "home shape" gate lives in the planner); anything else falls back to deleting
# known buckets individually. Used here only for the top-level-under-home guard.
OPENSQUILLA_HOME_NAMES = (".opensquilla", "opensquilla")

_PERSONAL_DIR_NAMES = (
    "Documents",
    "Desktop",
    "Downloads",
    "Pictures",
    "Music",
    "Movies",
    "Videos",
    "Library",
    "Dropbox",
    "OneDrive",
    "Google Drive",
    "Box",
)


def resolve_real(path: Path) -> Path:
    """Best-effort ``realpath`` that never raises (falls back to absolute)."""
    try:
        return path.resolve()
    except (OSError, ValueError, RuntimeError):
        return path.absolute()


def is_within(target: Path, root: Path) -> bool:
    """Return True if ``target`` resolves to a location at or under ``root``.

    Resolves symlinks on both sides first, so a symlinked target whose real
    location escapes ``root`` is correctly rejected.
    """
    t = resolve_real(target)
    r = resolve_real(root)
    if t == r:
        return True
    try:
        t.relative_to(r)
        return True
    except ValueError:
        return False


def home_dir() -> Path:
    """The invoking user's home, matching ``paths._home_dir`` ($HOME first)."""
    env_home = os.environ.get("HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser()
    return Path.home()


def protected_root_reason(path: Path) -> str | None:
    """Reason ``path`` must not be used as a blanket-deletion root, else ``None``.

    Used before any recursive ``rmtree`` of a *root* (the OpenSquilla home, a
    relocated state dir, a media root). Individual known leaf files inside a root
    are still deletable even when the root itself is protected.
    """
    resolved = resolve_real(path)

    anchor = resolved.anchor
    if anchor and str(resolved) == anchor:
        return "path is a filesystem root"

    components = [p for p in resolved.parts if p != anchor]
    if len(components) < _MIN_ROOT_DEPTH:
        return "path is too close to the filesystem root"

    # A mount point / volume root is never a legitimate single-app home; deleting
    # it wipes the whole volume (e.g. OPENSQUILLA_STATE_DIR=/Volumes/Drive).
    try:
        if os.path.ismount(str(resolved)):
            return "path is a mount point / volume root"
    except OSError:
        return "path could not be stat'd"
    if resolved.parent.name in _MOUNT_CONTAINER_NAMES or str(resolved.parent) == resolved.anchor:
        # Directly under /Volumes, /mnt, /media, /srv, or a drive/filesystem root.
        return "path is directly under a system mount container or filesystem root"

    home = resolve_real(home_dir())
    if resolved == home:
        return "path is the user's home directory"
    if is_within(home, resolved):
        # resolved is an ancestor of $HOME (e.g. /Users, /home).
        return "path is an ancestor of the user's home directory"

    # A direct child of $HOME that is not the OpenSquilla home is likely a
    # personal / sync / profile root (Dropbox, OneDrive, Library, localized
    # Documents) — refuse a blanket rmtree regardless of locale.
    try:
        rel = resolved.relative_to(home)
    except ValueError:
        rel = None
    if rel is not None and len(rel.parts) == 1 and rel.parts[0] not in OPENSQUILLA_HOME_NAMES:
        return "path is a top-level directory under the user's home"

    personal = {resolve_real(home / name) for name in _PERSONAL_DIR_NAMES}
    if resolved in personal:
        return "path is a personal directory (Documents / Library / sync root / ...)"

    return None


def is_protected_root(path: Path) -> bool:
    """True when ``path`` is too dangerous to recursively delete as a root."""
    return protected_root_reason(path) is not None


def looks_like_opensquilla_home(home: Path) -> bool:
    """Positive check that ``home`` is an OpenSquilla home (gates whole-tree rmtree).

    Required (in addition to :func:`protected_root_reason` being clear) before a
    ``--purge-all`` blanket ``rmtree`` of the home. To avoid green-lighting an
    arbitrary relocated directory, the signal must be UNAMBIGUOUS: the canonical
    leaf name (resolved), or the OpenSquilla-specific install receipt. Generic
    files like ``config.toml`` / ``state`` are intentionally NOT accepted — a
    relocated dir that merely contains them falls back to per-bucket removal.
    """
    if resolve_real(home).name in OPENSQUILLA_HOME_NAMES:
        return True
    try:
        return (home / "install-receipt.json").exists()
    except OSError:
        return False
