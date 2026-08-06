"""OpenSquilla — multi-channel LLM gateway and agent runtime."""

from __future__ import annotations

from pathlib import Path
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version


def _pyproject_version() -> str | None:
    """Return the ``version`` field from the nearest ``pyproject.toml``.

    Editable/source checkouts run from the repository tree, whose
    ``pyproject.toml`` is authoritative; installed dist metadata for editable
    installs can lag behind the source (the stale-version bug this fixes).
    """
    here = Path(__file__).resolve().parent
    # Cover both src-layout (repo root is two levels up) and flat-layout
    # (repo root is one level up) source checkouts.
    for candidate in (here, here.parent, here.parent.parent):
        pyproject = candidate / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            import tomllib
        except ImportError:  # pragma: no cover - Python < 3.11
            return None
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            version = (data.get("project") or {}).get("version")
            if isinstance(version, str) and version:
                return version
        except (OSError, ValueError):  # pragma: no cover - unreadable/invalid TOML
            return None
        return None
    return None


def _resolve_version() -> str:
    """Resolve the effective OpenSquilla version for this checkout."""
    source_version = _pyproject_version()
    if source_version:
        return source_version
    try:
        return _dist_version("opensquilla")
    except PackageNotFoundError:  # pragma: no cover - source tree without dist metadata
        # Fall back to a sentinel rather than a hardcoded semver that silently
        # goes stale (the bug this module exists to prevent).
        return "0.0.0+unknown"


__version__ = _resolve_version()

__all__ = ["__version__"]
