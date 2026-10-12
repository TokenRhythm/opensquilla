"""OpenSquilla — multi-channel LLM gateway and agent runtime."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _resolve_version

try:
    __version__ = _resolve_version("opensquilla")
except PackageNotFoundError:  # pragma: no cover - source tree without dist metadata
    # Editable/source checkouts without installed distribution metadata fall
    # back to a sentinel rather than a hardcoded semver that silently goes
    # stale (the bug this module exists to prevent).
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
