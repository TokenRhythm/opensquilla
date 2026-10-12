"""Filesystem cleanup for session-owned material on session delete.

``SQLiteSessionStorage.delete_session`` removes DB rows only. The two on-disk
session-material stores must be removed too, or they accumulate forever (session
delete leaves them behind and the global transcript disk budget eventually
hard-fails every new staged attachment):

1. the canonical transcript-material store ``<media_root>/transcripts/<sid>/``;
2. the tool-visible workspace materialization
   ``<workspace>/.opensquilla/attachments/<segment>/``.

The cleanup is a process-global hook (matching the ``set_upload_store`` pattern)
so every ``delete_session`` caller — the ``sessions.delete`` RPC, the cron
reaper, and ``prune_stale_sessions`` — triggers it through the single DB choke
point without threading config through the low-level storage class.

This module intentionally holds only the hook registry and a generic, guarded
directory remover — no config/workspace resolution — so the low-level
``session`` package does not depend on higher layers (``agents``, gateway). The
concrete cleanup that resolves the media root + agent workspace is built at boot
(see ``gateway.boot.build_session_material_cleanup``) and registered here.
"""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

SessionMaterialCleanup = Callable[[str, str], Awaitable[None]]

_hook: SessionMaterialCleanup | None = None


def set_session_material_cleanup(hook: SessionMaterialCleanup | None) -> None:
    """Register (or clear) the process-global session-material cleanup hook."""
    global _hook
    _hook = hook


def reset_session_material_cleanup() -> None:
    """Test hook — drop the registered cleanup."""
    global _hook
    _hook = None


async def run_session_material_cleanup(session_id: str, session_key: str) -> None:
    """Invoke the registered cleanup for a deleted session, if any.

    Best-effort: a cleanup failure is logged but never propagated, so a
    filesystem hiccup cannot block a session delete.
    """
    hook = _hook
    if hook is None:
        return
    try:
        await hook(session_id, session_key)
    except Exception as exc:  # noqa: BLE001 — cleanup must never fail the delete
        log.warning(
            "session_material_cleanup.failed",
            session_id=session_id,
            session_key=session_key,
            error=str(exc),
        )


def is_safe_segment(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and "/" not in name and "\\" not in name


def rmtree_scoped(target: Path, *, expected_name: str) -> None:
    """Remove ``target`` only if it is a directory whose final segment matches.

    The segment guard prevents a malformed session id/segment from escaping its
    parent (e.g. ``..`` or an absolute path) and deleting something unrelated.
    """
    if not is_safe_segment(expected_name) or target.name != expected_name:
        log.warning("session_material_cleanup.unsafe_target", target=str(target))
        return
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target, ignore_errors=True)
