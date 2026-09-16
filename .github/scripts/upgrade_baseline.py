"""Pinned v0.5.4 schema/ledger for release gates (standard library only)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/upgrade-v054"
SOURCE_SHA = "3877f9668c527a2a74c9b72bab160155669b040d"


def manifest() -> dict:
    result = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
    if result["source_sha"] != SOURCE_SHA or len(result["ledger"]) != 41:
        raise ValueError("v0.5.4 baseline provenance or complete ledger is invalid")
    return result


def schema_sql() -> str:
    payload = (FIXTURE / "sessions.sql").read_bytes()
    if hashlib.sha256(payload).hexdigest() != manifest()["sql_sha256"]:
        raise ValueError("v0.5.4 baseline SQL digest mismatch")
    return payload.decode("utf-8")


def read_ledger(database: Path) -> dict[str, str]:
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return dict(connection.execute(
            "SELECT migration_id, migration_hash FROM _yoyo_migration ORDER BY migration_id"
        ))


def verify_ledger(database: Path, *, exact: bool = False) -> dict[str, str]:
    expected = manifest()["ledger"]
    actual = read_ledger(database)
    changed = sorted(key for key, value in expected.items() if actual.get(key) != value)
    if changed or (exact and actual != expected):
        raise AssertionError(f"v0.5.4 migration ledger was not retained: {changed or 'extra IDs'}")
    return actual
