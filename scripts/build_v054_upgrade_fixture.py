"""Regenerate the synthetic v0.5.4 upgrade fixture from its immutable Git source.

Run with the repository development environment. Never reads a user profile.
The runtime gate consumes the resulting SQL using only the Python standard library.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

SOURCE_SHA = "3877f9668c527a2a74c9b72bab160155669b040d"
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests/fixtures/upgrade-v054"


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def main() -> None:
    import sqlite3

    with tempfile.TemporaryDirectory(prefix="opensquilla-v054-fixture-") as temporary:
        work = Path(temporary)
        os.environ["OPENSQUILLA_USER_STATE_DIR"] = str(work / "user-state")
        os.environ["OPENSQUILLA_TEST_PROFILE_LOCK_ROOT"] = "1"
        from opensquilla.persistence.migrator import apply_pending

        migrations = work / "migrations"
        migrations.mkdir()
        inventory = {}
        for name in git("ls-tree", "--name-only", SOURCE_SHA + ":migrations").decode().splitlines():
            if not name.startswith("V") or not name.endswith(".py"):
                continue
            payload = git("show", f"{SOURCE_SHA}:migrations/{name}")
            (migrations / name).write_bytes(payload)
            inventory[name] = hashlib.sha256(payload).hexdigest()
        assert len(inventory) == 41
        storage = git("show", f"{SOURCE_SHA}:src/opensquilla/session/storage.py")
        statements = []
        for node in ast.parse(storage).body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                value = node.value.value
                if isinstance(value, str) and any(
                    f"CREATE TABLE IF NOT EXISTS {table} (" in value
                    for table in ("sessions", "transcript_entries")
                ):
                    statements.append(value)
        assert len(statements) == 2
        database = work / "sessions.db"
        with closing(sqlite3.connect(database)) as connection:
            for statement in statements:
                connection.execute(statement)
        # Do not publish the builder's username, hostname or MAC-derived UUIDs
        # in the otherwise fully synthetic audit rows. Ledger IDs stay intact.
        with (
            patch("getpass.getuser", return_value="upgrade-fixture"),
            patch("socket.gethostname", return_value="fixture-host"),
            patch("uuid.uuid1", side_effect=uuid.uuid4),
        ):
            assert len(apply_pending(str(database), migrations)) == 41
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            ledger = connection.execute(
                "SELECT migration_id, migration_hash FROM _yoyo_migration ORDER BY migration_id"
            ).fetchall()
            sql = "\n".join(connection.iterdump()) + "\n"
        OUTPUT.mkdir(parents=True, exist_ok=True)
        payload = sql.encode("utf-8")
        (OUTPUT / "sessions.sql").write_bytes(payload)
        (OUTPUT / "manifest.json").write_text(
            json.dumps({
                "baseline_version": "0.5.4",
                "source_sha": SOURCE_SHA,
                "storage_source_sha256": hashlib.sha256(storage).hexdigest(),
                "sql_sha256": hashlib.sha256(payload).hexdigest(),
                "migration_files": inventory,
                "ledger": dict(ledger),
            }, indent=2) + "\n", encoding="utf-8",
        )


if __name__ == "__main__":
    main()
