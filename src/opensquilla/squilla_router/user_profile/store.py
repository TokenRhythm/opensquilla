"""Versioned on-disk store for offline-produced user profiles.

Layout under the per-agent router data dir (reusing ``self_learning.store``'s
root and agent-id sanitizer, never the repo, never the decision log)::

    ~/.opensquilla/router/data/<agent_id>/profiles/
        user_profile.2026-07-10.1.json   # a produced version (kept, never overwritten)
        user_profile.2026-07-09.1.json
        active                           # one line: the active version filename

The ``active`` pointer here is *independent* of ``self_learning``'s
``router/active`` bundle pointer — a different artifact with a different
lifecycle. Version files are immutable and the active-pointer update is atomic;
reads fail open to ``None`` so a missing/corrupt profile degrades to the mock
baseline.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from opensquilla.squilla_router.self_learning.store import agent_data_dir

PROFILE_PREFIX = "user_profile"
ACTIVE_POINTER = "active"

# ``user_profile.<YYYY-MM-DD>.<N>.json`` — the version is the date + a per-day
# sequence, matching the offline doc's filename scheme (§1.6).
_VERSION_FILE_RE = re.compile(r"^user_profile\.(?P<date>\d{4}-\d{2}-\d{2})\.(?P<seq>\d+)\.json$")


def profiles_dir(agent_id: str, home: Path | None = None) -> Path:
    """The per-agent directory holding produced profile versions."""

    return agent_data_dir(agent_id, home) / "profiles"


def active_pointer_path(agent_id: str, home: Path | None = None) -> Path:
    return profiles_dir(agent_id, home) / ACTIVE_POINTER


def version_filename(version: str) -> str:
    return f"{PROFILE_PREFIX}.{version}.json"


def next_version(day: str, agent_id: str, home: Path | None = None) -> str:
    """Return ``<day>.<N>`` where N is the next unused sequence for ``day``.

    Scans existing files so a same-day re-run bumps the sequence rather than
    overwriting a prior version (§1.6: history is never clobbered).
    """

    directory = profiles_dir(agent_id, home)
    highest = 0
    if directory.is_dir():
        for path in directory.glob(f"{PROFILE_PREFIX}.{day}.*.json"):
            match = _VERSION_FILE_RE.match(path.name)
            if match is None or match.group("date") != day:
                continue
            highest = max(highest, int(match.group("seq")))
    return f"{day}.{highest + 1}"


def write_profile_version(
    payload: dict,
    version: str,
    agent_id: str,
    *,
    home: Path | None = None,
) -> Path:
    """Write one immutable version file; return its path. Never overwrites."""

    directory = profiles_dir(agent_id, home)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / version_filename(version)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def write_active_atomic(version: str, agent_id: str, *, home: Path | None = None) -> None:
    """Atomically point ``active`` at ``user_profile.<version>.json``."""

    path = active_pointer_path(agent_id, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(version_filename(version), encoding="utf-8")
    os.replace(tmp, path)


def read_active_name(agent_id: str, home: Path | None = None) -> str | None:
    path = active_pointer_path(agent_id, home)
    if not path.is_file():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def load_active_profile(agent_id: str, home: Path | None = None) -> dict | None:
    """The active produced profile as a raw dict (with ``_meta``), or ``None``.

    ``None`` covers every failure — no pointer, dangling pointer, malformed
JSON, wrong shape — so callers can degrade to their own defaults. Never raises:
a broken produced profile must not fail a turn.
    """

    name = read_active_name(agent_id, home)
    if not name:
        return None
    # The pointer names a bare filename inside profiles/; reject anything with a
    # path separator so a corrupt pointer cannot escape the directory.
    if name != Path(name).name:
        return None
    path = profiles_dir(agent_id, home) / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


__all__ = [
    "ACTIVE_POINTER",
    "PROFILE_PREFIX",
    "active_pointer_path",
    "load_active_profile",
    "next_version",
    "profiles_dir",
    "read_active_name",
    "version_filename",
    "write_active_atomic",
    "write_profile_version",
]
