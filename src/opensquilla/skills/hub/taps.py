"""Tap system — custom skill source repositories (Homebrew-style)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import structlog

from opensquilla.skills.paths import default_taps_file, legacy_taps_file

log = structlog.get_logger(__name__)


@dataclass
class Tap:
    """A registered skill source repository."""

    owner: str
    repo: str
    url: str = ""
    added_at: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def _default_taps_path() -> Path:
    return default_taps_file()


def _migrate_legacy_taps(new_path: Path) -> Path:
    """Return the taps path to use, migrating the legacy file when possible.

    Happy path: relocates the legacy state tap file to ``new_path`` via
    :func:`os.replace` (atomic on POSIX) and returns ``new_path``.

    Failure fallback: when the rename fails (read-only filesystem, permission
    error, EXDEV across filesystems) this logs a warning and returns the
    legacy path so :class:`TapsManager` keeps reading and writing the existing
    data in place. Silent migration loss is unacceptable — taps tell the
    installer which repos to trust.
    """
    old = legacy_taps_file()
    if new_path.exists() or not old.exists():
        return new_path
    try:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old, new_path)
        return new_path
    except OSError as exc:
        log.warning(
            "skills.taps.migration_failed",
            legacy=str(old),
            new=str(new_path),
            error=str(exc),
        )
        return old


class TapsManager:
    """Manages custom skill source taps."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            path = _migrate_legacy_taps(_default_taps_path())
        self._path = path
        self._taps: dict[str, Tap] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for entry in data.get("taps", []):
                tap = Tap(**entry)
                self._taps[tap.full_name] = tap
        except (json.JSONDecodeError, TypeError, OSError):
            pass

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"taps": [asdict(t) for t in self._taps.values()]}
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add(self, owner_repo: str) -> Tap:
        """Add a tap by owner/repo string. Returns the Tap."""
        import time

        parts = owner_repo.strip().split("/")
        if len(parts) != 2:
            raise ValueError(f"Invalid tap format: '{owner_repo}'. Expected 'owner/repo'.")
        owner, repo = parts
        tap = Tap(
            owner=owner,
            repo=repo,
            url=f"https://github.com/{owner}/{repo}",
            added_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self._taps[tap.full_name] = tap
        self._save()
        return tap

    def remove(self, owner_repo: str) -> bool:
        """Remove a tap. Returns True if it existed."""
        if owner_repo in self._taps:
            del self._taps[owner_repo]
            self._save()
            return True
        return False

    def list(self) -> list[Tap]:
        """Return all registered taps."""
        return list(self._taps.values())

    def get(self, owner_repo: str) -> Tap | None:
        return self._taps.get(owner_repo)
