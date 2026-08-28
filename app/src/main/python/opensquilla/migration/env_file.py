"""Shared helpers for preserving hand-written `.env` files during migration."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def env_line_key(text: str) -> str | None:
    stripped = text.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    key, _, _ = stripped.partition("=")
    return key.strip().lstrip("\ufeff")


def merge_env_lines(existing_lines: list[str], additions: dict[str, str]) -> list[str]:
    lines: list[str] = []
    consumed: set[str] = set()
    for line in existing_lines:
        key = env_line_key(line)
        if key is not None and key in additions:
            if key not in consumed:
                lines.append(f"{key}={additions[key]}")
                consumed.add(key)
            continue
        lines.append(line)
    for key, value in sorted(additions.items()):
        if key not in consumed:
            lines.append(f"{key}={value}")
    return lines


def write_secret_env_file(env_path: Path, lines: list[str]) -> None:
    """Atomically write ``lines`` to ``env_path`` with owner-only permissions.

    The temp file comes from ``mkstemp`` (created 0600 on POSIX) in the
    destination directory, so the secret bytes never exist on disk with
    umask-default permissions; ``os.replace`` then swaps it into place so a
    crash mid-write cannot leave a truncated ``.env``. Failures propagate to
    the caller after the temp file has been removed \u2014 a secret write that
    cannot be completed securely must never be silently swallowed.

    Two compatibility guarantees on top of the atomic swap:

    - A symlinked ``env_path`` (dotfiles-managed setups) is resolved first so
      the swap replaces the link *target*, not the link \u2014 mirroring the
      write-through behavior of ``onboarding.config_store.persist_config``.
    - When the containing directory is not writable (``mkstemp`` fails with
      ``PermissionError``) but the file itself exists and is writable, the
      write falls back to an in-place rewrite so previously-succeeding
      migrations do not start failing partway through; the atomic guarantee
      is best-effort in that degraded case.
    """
    target = env_path
    if target.is_symlink():
        target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines) + "\n"
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
    except PermissionError:
        if not target.is_file():
            raise
        # Directory not writable, file writable: preserve the pre-atomic
        # behavior (plain in-place write) instead of failing the migration.
        with target.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
