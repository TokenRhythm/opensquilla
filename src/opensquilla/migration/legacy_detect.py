"""Read-only discovery of importable OpenSquilla profile homes.

Discovery is deliberately demand-driven.  The settings migration surface asks
for candidates against the gateway's *actual* target home; doctor and
onboarding no longer call this module.  Gateway boot keeps one advisory,
log-only hint (a fresh home beside importable legacy data) so headless
operators still learn their old profile exists — execution stays behind the
CLI and the settings surface.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

#: Env vars that ever hosted a Windows-portable data dir; when neither is set
#: (and the platform is not Windows) portable enumeration is skipped entirely.
_PORTABLE_BASE_ENV_VARS = ("LOCALAPPDATA", "TEMP")
_MAX_DISCOVERED_HOMES = 12


@dataclass(frozen=True)
class LegacyHomeCandidate:
    """One importable legacy home: where it lives and which source kind it is."""

    path: Path
    kind: str


def detect_legacy_home(target: Path | None = None) -> LegacyHomeCandidate | None:
    """Return the most likely legacy OpenSquilla home distinct from ``target``.

    ``target`` defaults to :func:`~opensquilla.paths.default_opensquilla_home`.
    A legacy CLI home (``~/.opensquilla``) wins over the platform desktop
    home, which wins over Windows-portable data dirs. Portable enumeration
    only runs where such dirs can exist (Windows, or a ``LOCALAPPDATA``/``TEMP``
    env base being present) and offers the newest candidate.

    Read-only and exception-free by contract: advisory callers must never
    fail because detection hit an unreadable disk, so any ``OSError``
    collapses to ``None``.
    """
    from opensquilla.paths import default_opensquilla_home

    resolved_target = target if target is not None else default_opensquilla_home()
    candidates = detect_legacy_homes(resolved_target, limit=1)
    return candidates[0] if candidates else None


def detect_legacy_homes(
    target: Path,
    *,
    limit: int = _MAX_DISCOVERED_HOMES,
) -> list[LegacyHomeCandidate]:
    """Return importable homes in deterministic product-priority order.

    CLI home comes first, followed by the platform Desktop home and then
    Windows Portable homes newest-first.  Duplicate directory objects are
    omitted and the public settings surface is bounded to twelve candidates.

    ``target`` is intentionally required.  Callers that own a running gateway
    must not silently fall back to an ambient shell home.
    """

    bounded_limit = max(0, min(int(limit), _MAX_DISCOVERED_HOMES))
    if bounded_limit == 0:
        return []
    try:
        from opensquilla.migration.opensquilla_home import (
            _advisory_identity,
            _same_path,
            detect_desktop_home,
            detect_legacy_cli_home,
            enumerate_portable_homes,
        )

        resolved_target = target.expanduser().absolute()
        ordered: list[LegacyHomeCandidate] = []
        cli_home = detect_legacy_cli_home(resolved_target)
        if cli_home is not None:
            ordered.append(LegacyHomeCandidate(path=cli_home, kind="cli-home"))

        desktop_home = detect_desktop_home(resolved_target)
        if desktop_home is not None and not _same_path(desktop_home, resolved_target):
            ordered.append(
                LegacyHomeCandidate(path=desktop_home, kind="desktop-home")
            )

        if sys.platform == "win32" or _portable_bases_present():
            for portable_candidate in enumerate_portable_homes(target=resolved_target):
                if not _same_path(portable_candidate.path, resolved_target):
                    ordered.append(
                        LegacyHomeCandidate(
                            path=portable_candidate.path,
                            kind="windows-portable",
                        )
                    )

        unique: list[LegacyHomeCandidate] = []
        seen_identities: set[object] = set()
        seen_paths: set[str] = set()
        for ordered_candidate in ordered:
            try:
                identity = _advisory_identity(ordered_candidate.path.lstat())
            except OSError:
                continue
            path_key = os.path.normcase(
                os.path.normpath(str(ordered_candidate.path.expanduser().absolute()))
            )
            if identity is not None and identity in seen_identities:
                continue
            if path_key in seen_paths:
                continue
            if identity is not None:
                seen_identities.add(identity)
            seen_paths.add(path_key)
            unique.append(ordered_candidate)
            if len(unique) >= bounded_limit:
                break
        return unique
    except OSError:
        return []


def _portable_bases_present() -> bool:
    return any(os.environ.get(name, "").strip() for name in _PORTABLE_BASE_ENV_VARS)
