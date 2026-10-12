"""Per-machine preferences for the OpenTUI-backed terminal surface.

One small schema-versioned JSON file (``<state>/tui/prefs.json``) holds state
that must survive restarts but is deliberately NOT gateway config: it
describes this terminal, not the agent, and gateway config may live on
another machine entirely. It carries the persisted ``/theme`` choice and the
shown-once bookkeeping for the plain-mode fallback notice — the launch
adapter records that notice here because the notice is itself about OpenTUI
availability on this machine.

Failure posture mirrors ``opensquilla.onboarding.probe_history``: absence or
corruption degrades to defaults, writes are atomic and best effort (logged,
swallowed) — a read-only state dir must never break or delay chat launch. Two
extra guards protect the read-modify-write cycle: an advisory lock serializes
concurrent chat processes, and a file that exists but cannot be READ blocks
the write entirely, so a transient EACCES/EIO can never clobber a good file
with a truncated one. The acceptable failure mode is a repeated notice or an
unsaved preference, never a crash and never lost sibling keys.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import IO, Any, cast

import structlog

from opensquilla import paths

log = structlog.get_logger(__name__)

_PREFS_FILENAME = "prefs.json"
_SCHEMA_VERSION = 1
# Bounded history for the fallback notice: enough that alternating between a
# few (version, reason) pairs never re-nags, small enough to stay one screen.
_MAX_FALLBACK_NOTICES = 8


def _prefs_path() -> Path:
    return paths.state_dir() / "tui" / _PREFS_FILENAME


# Non-blocking lock probes: enough to ride out a sibling's microsecond-scale
# read-modify-write, bounded so a wedged holder costs ~100ms, never a hang.
_LOCK_ATTEMPTS = 5
_LOCK_RETRY_SECONDS = 0.02


def _try_lock_once(handle: IO[bytes]) -> bool:
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import msvcrt  # noqa: PLC0415

        msvcrt_mod = cast(Any, msvcrt)
        handle.seek(0)
        msvcrt_mod.locking(handle.fileno(), msvcrt_mod.LK_NBLCK, 1)
        return True
    import fcntl  # noqa: PLC0415

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return True


@contextmanager
def _advisory_lock() -> Iterator[None]:
    """Best-effort cross-process lock for the read-modify-write cycle.

    Uses a sidecar lock file with the same OS primitives as
    ``opensquilla.onboarding.config_store``, but strictly NON-BLOCKING: a held
    or unobtainable lock degrades to unlocked best-effort behavior after a few
    bounded probes. A wedged sibling process must never be able to stall chat
    launch or the /theme path behind this file.
    """
    handle: IO[bytes] | None = None
    locked = False
    lock_path = _prefs_path().with_suffix(".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+b")  # noqa: SIM115 - explicit unlock ordering
        for attempt in range(_LOCK_ATTEMPTS):
            try:
                locked = _try_lock_once(handle)
                break
            except OSError:
                if attempt + 1 < _LOCK_ATTEMPTS:
                    time.sleep(_LOCK_RETRY_SECONDS)
    except OSError:
        pass
    try:
        yield
    finally:
        if handle is not None:
            if locked:
                with suppress(OSError):
                    if os.name == "nt":  # pragma: no cover - Windows only
                        import msvcrt  # noqa: PLC0415

                        msvcrt_mod = cast(Any, msvcrt)
                        handle.seek(0)
                        msvcrt_mod.locking(handle.fileno(), msvcrt_mod.LK_UNLCK, 1)
                    else:
                        import fcntl  # noqa: PLC0415

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            with suppress(OSError):
                handle.close()


def _read() -> tuple[dict[str, Any], bool]:
    """Return ``(prefs, writable)``.

    ``writable`` is False only when the file exists but could not be read
    (EACCES/EIO): the on-disk data may still be good, so overwriting from the
    empty fallback would destroy it. A missing file or a corrupt one (nothing
    recoverable) keeps writes enabled.
    """
    path = _prefs_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, True
    except OSError as exc:
        log.warning("tui.prefs.read_failed", error=str(exc))
        return {}, False
    except ValueError:
        return {}, True
    if not isinstance(raw, dict):
        return {}, True
    raw.pop("schemaVersion", None)
    return raw, True


def _load() -> dict[str, Any]:
    prefs, _writable = _read()
    return prefs


def _store(prefs: dict[str, Any]) -> None:
    path = _prefs_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schemaVersion": _SCHEMA_VERSION, **prefs}
        fd, tmp_name = tempfile.mkstemp(prefix=_PREFS_FILENAME, dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_name, path)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError as exc:
        log.warning("tui.prefs.write_failed", error=str(exc))


def load_theme_preference() -> str | None:
    """Return the persisted theme name, or ``None`` when unset or unknown.

    Unknown names are ignored rather than surfaced: after a downgrade or a
    theme rename, a stale preference silently falls back to the default
    instead of failing launch over a cosmetic setting.
    """
    from opensquilla.cli.tui.opentui.themes import THEME_NAMES  # noqa: PLC0415

    name = str(_load().get("theme") or "").strip().lower()
    return name if name in THEME_NAMES else None


def save_theme_preference(name: str) -> None:
    """Persist a confirmed theme choice (best effort, validated)."""
    from opensquilla.cli.tui.opentui.themes import THEME_NAMES  # noqa: PLC0415

    cleaned = str(name or "").strip().lower()
    if cleaned not in THEME_NAMES:
        return
    with _advisory_lock():
        prefs, writable = _read()
        if not writable or prefs.get("theme") == cleaned:
            return
        prefs["theme"] = cleaned
        _store(prefs)


def _notice_records(prefs: dict[str, Any]) -> list[dict[str, Any]]:
    records = prefs.get("fallbackNotices")
    if isinstance(records, list):
        return [record for record in records if isinstance(record, dict)]
    # One legacy singular record (earliest file layout) still counts.
    legacy = prefs.get("fallbackNotice")
    return [legacy] if isinstance(legacy, dict) else []


def fallback_notice_due(product_version: str, reason_code: str) -> bool:
    """True when the plain-mode fallback line was not yet shown for this
    (product version, unavailability reason) pair.

    The reason can change without an upgrade (e.g. ``missing`` becomes
    ``version_mismatch``), and a changed reason is exactly when the user wants
    one fresh line — but a previously seen pair stays quiet, so alternating
    reasons never turn into a per-launch nag. Read failures fail open: the
    notice repeats, which degrades loud rather than silent.
    """
    return not any(
        str(record.get("productVersion") or "") == product_version
        and str(record.get("reasonCode") or "") == reason_code
        for record in _notice_records(_load())
    )


def record_fallback_notice(product_version: str, reason_code: str) -> None:
    """Append the shown (version, reason) pair to the bounded record."""
    with _advisory_lock():
        prefs, writable = _read()
        if not writable:
            return
        records = _notice_records(prefs)
        entry = {"productVersion": product_version, "reasonCode": reason_code}
        if entry in records:
            return
        records.append(entry)
        prefs.pop("fallbackNotice", None)
        prefs["fallbackNotices"] = records[-_MAX_FALLBACK_NOTICES:]
        _store(prefs)
