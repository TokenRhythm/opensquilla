"""Shared, read-only classification of an existing migration ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

# Released artifact renumbering is the only supported historical alias chain.
# These are yoyo identity hashes, not hashes of migration source code.
LEGACY_MIGRATION_ALIASES: dict[str, tuple[str, str]] = {
    "V036__artifact_sessions": (
        "V037__artifact_sessions",
        "629c36c68995c8ad03b3ede54698658fa4ca1885bc66bca257b2961d5e01df4e",
    ),
    "V037__artifact_prompt_annotations": (
        "V038__artifact_prompt_annotations",
        "adeb19ffbc8c3dd64921b8282daec79e8cbf875ef010dacf00a41cbcee637ce7",
    ),
    "V038__artifact_mutation_attempts": (
        "V039__artifact_mutation_attempts",
        "9775f8aa161c6172d3f0010a6016d2881894f7b0bc504c59ca79f3e67138b11c",
    ),
    "V039__document_resources": (
        "V040__document_resources",
        "d3c739ce2989470f11cd8a8d8aef811a1e12fa7ac6dbc4dfccd5ce7323762390",
    ),
}
REGISTRY_FILENAME = "registry.json"


@dataclass(frozen=True)
class MigrationCompatibility:
    code: str | None
    conflicting_ids: tuple[str, ...] = ()


def classify_migration_ledger(
    applied: Mapping[str, str | None],
    known: Collection[str],
    *,
    aliases: Mapping[str, tuple[str, str]] = LEGACY_MIGRATION_ALIASES,
) -> MigrationCompatibility:
    """Recognize exact supported aliases without inventing migration ancestry."""

    legacy_goals = tuple(sorted(
        item for item in applied
        if item == "V029__goal_runs" or item.startswith("V030__goal_")
    ))
    if legacy_goals:
        return MigrationCompatibility("state_unsupported_goal_lineage", legacy_goals)
    if not known:
        return MigrationCompatibility("state_migration_set_unavailable")
    unknown: list[str] = []
    for migration_id, recorded_hash in applied.items():
        alias = aliases.get(migration_id)
        if alias is not None and alias[0] in known:
            if recorded_hash != alias[1]:
                return MigrationCompatibility("state_migration_alias_mismatch", (migration_id,))
        elif migration_id not in known:
            unknown.append(migration_id)
    if unknown:
        return MigrationCompatibility("state_schema_too_new", tuple(sorted(unknown)))
    return MigrationCompatibility(None)


def read_migration_ledger(connection: sqlite3.Connection) -> dict[str, str | None]:
    """Inspect an already-open safe connection, including legacy id-only ledgers."""

    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%yoyo_migration'"
    ).fetchall()
    table = next((str(row[0]) for row in tables if str(row[0]).endswith("yoyo_migration")), None)
    if table is None:
        return {}
    quoted = table.replace('"', '""')
    columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{quoted}")')}
    hash_column = "migration_hash" if "migration_hash" in columns else "NULL"
    return {
        str(migration_id): str(value) if value is not None else None
        for migration_id, value in connection.execute(
            f'SELECT migration_id, {hash_column} FROM "{quoted}"'
        )
        if migration_id
    }


def frozen_migration_registry(directory: Path) -> dict[str, str] | None:
    """Read the small build-time inventory; never discover migrations at boot."""

    registry = directory / REGISTRY_FILENAME
    if not registry.is_file():
        return None
    payload = json.loads(registry.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Unsupported frozen migration registry")
    migrations = payload.get("migrations")
    if not isinstance(migrations, dict) or not migrations:
        raise ValueError("Frozen migration registry has no migrations")
    result: dict[str, str] = {}
    for migration_id, entry in migrations.items():
        expected = hashlib.sha256(migration_id.encode("utf-8")).hexdigest()
        if (
            not migration_id.startswith("V")
            or "/" in migration_id or "\\" in migration_id
            or not isinstance(entry, dict)
            or entry.get("ledger_hash") != expected
            or not isinstance(entry.get("source_sha256"), str)
            or len(entry["source_sha256"]) != 64
        ):
            raise ValueError("Invalid frozen migration registry entry")
        result[migration_id] = expected
    return result


def known_migration_ids(directory: Path) -> set[str]:
    registry = frozen_migration_registry(directory)
    if registry is not None:
        return set(registry)
    # Source development and explicit custom migration directories retain the
    # existing discovery behavior. Release builds always freeze an inventory.
    return {entry.stem for entry in directory.glob("V*.py") if entry.is_file()}
