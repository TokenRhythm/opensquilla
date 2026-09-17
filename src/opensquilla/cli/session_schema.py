"""Session-schema preparation composed above the offline recovery layer."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path


async def _initialize_current_session_schema(path: Path) -> None:
    """Run SessionStorage's current-schema initialization and column shims."""

    from opensquilla.recovery.atomic import _native_io_path
    from opensquilla.session.storage import SessionStorage

    storage = SessionStorage(_native_io_path(path))
    try:
        await storage.connect()
    finally:
        await storage.close()


def _run_session_schema_initialization(path: Path) -> None:
    """Bridge SessionStorage's async initializer into the synchronous CLI."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_initialize_current_session_schema(path))
        return

    errors: list[BaseException] = []

    def initialize_in_thread() -> None:
        try:
            asyncio.run(_initialize_current_session_schema(path))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(
        target=initialize_in_thread,
        name="opensquilla-session-schema-upgrade",
    )
    worker.start()
    worker.join()
    if errors:
        raise errors[0]


def prepare_session_schema(path: Path) -> None:
    """Apply migrations and SessionStorage shims before an offline merge."""

    from opensquilla.persistence.migrator import apply_pending, resolve_migrations_dir
    from opensquilla.recovery.atomic import _native_io_path

    apply_pending(_native_io_path(path), resolve_migrations_dir())
    _run_session_schema_initialization(path)


__all__ = ["prepare_session_schema"]
