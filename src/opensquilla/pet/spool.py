"""Offline notification spool compatible with the native ``importSpool()``.

When the pet is unreachable, notifications are appended to a JSONL file at the
same location/format the external bridge used (PRD §8.1), so the native side
replays them on startup / every few seconds (work-item only, no sound). The
file is capped at ~1MB by keeping the newest lines, so a long-offline pet never
grows it without bound. A pause file mutes notifications entirely.

Stdlib-only; all filesystem errors are swallowed because a notification must
never block or crash a session turn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_SPOOL_FILENAME = "pending-events.jsonl"
_PAUSE_FILENAME = "opensquilla-pet-bridge.pause"
_KEEP_LINES = 500


def default_spool_dir() -> Path:
    """Legacy bridge spool directory: ``%APPDATA%/LIG/AgentRMCNative``."""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path(os.environ.get("TMPDIR", "/tmp"))
    return base / "LIG" / "AgentRMCNative"


def default_pause_file() -> Path:
    """Legacy bridge pause file: ``%TEMP%/opensquilla-pet-bridge.pause``."""
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or "."
    return Path(temp) / _PAUSE_FILENAME


def resolve_spool_dir(configured: str) -> Path:
    return Path(configured).expanduser() if configured.strip() else default_spool_dir()


def resolve_pause_file(configured: str) -> Path:
    return Path(configured).expanduser() if configured.strip() else default_pause_file()


def is_paused(pause_file: Path) -> bool:
    try:
        return pause_file.exists()
    except OSError:
        return False


class Spool:
    """Append-only JSONL spool with a byte cap (keep newest lines)."""

    def __init__(self, directory: Path, *, cap_bytes: int = 1_048_576) -> None:
        self._dir = directory
        self._cap = max(4096, int(cap_bytes))
        self.path = directory / _SPOOL_FILENAME

    def _trim(self) -> None:
        try:
            if self.path.stat().st_size <= self._cap:
                return
            lines = [
                line
                for line in self.path.read_text(encoding="utf-8").split("\n")
                if line
            ]
            self.path.write_text("\n".join(lines[-_KEEP_LINES:]) + "\n", encoding="utf-8")
        except OSError:
            # If trimming fails, keep appending; never block a notification.
            pass

    def append(self, payload: dict[str, Any]) -> bool:
        """Persist one notification; return True on success (best-effort)."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._trim()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return True
        except OSError:
            return False

    def read_pending(self) -> list[dict[str, Any]]:
        """Read spooled notifications (for status/tests); tolerate bad lines."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        items: list[dict[str, Any]] = []
        for line in raw.split("\n"):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                items.append(value)
        return items


__all__ = [
    "Spool",
    "default_pause_file",
    "default_spool_dir",
    "is_paused",
    "resolve_pause_file",
    "resolve_spool_dir",
]
