"""Apply the frozen schema used by historical migration regression tests."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from yoyo import get_backend, read_migrations


def apply_pre_retirement_migrations(db_path: str, migrations_dir: Path) -> list[str]:
    """Test earlier recreate/copy migrations before V047 retires their tables."""
    sqlite3.register_adapter(datetime, lambda value: value.isoformat(" "))
    backend = get_backend(f"sqlite:///{db_path}")
    try:
        migrations = read_migrations(str(migrations_dir)).filter(
            lambda migration: migration.id != "V047__retire_product_modes"
        )
        with backend.lock():
            pending = backend.to_apply(migrations)
            backend.apply_migrations(pending)
        return [migration.id for migration in pending]
    finally:
        backend.connection.close()
