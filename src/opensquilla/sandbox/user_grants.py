"""Durable user-level sandbox grant storage."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from opensquilla.paths import native_io_path, state_dir

_STATE_FILE = "sandbox_user_grants.sqlite"
_LEGACY_STATE_FILE = "sandbox_user_grants.json"
_KINDS = ("mounts", "domains", "bundles", "public_network")
_KEY_FIELDS = {
    "mounts": "path",
    "domains": "domain",
    "bundles": "bundle_id",
    "public_network": "scope",
}


def load_user_grants_payload() -> dict[str, list[dict[str, Any]]]:
    payload: dict[str, list[dict[str, Any]]] = {kind: [] for kind in _KINDS}
    with closing(_connect()) as conn:
        for row in conn.execute(
            "SELECT kind, payload FROM sandbox_user_grants ORDER BY rowid"
        ):
            kind = str(row["kind"])
            if kind not in payload:
                continue
            item = _decode_payload(row["payload"])
            if item is not None:
                payload[kind].append(item)
    return payload


def upsert_domain_grant(payload: dict[str, Any]) -> None:
    _upsert("domains", "domain", payload)


def remove_domain_grant(domain: str) -> None:
    _remove("domains", domain)


def upsert_mount_grant(payload: dict[str, Any]) -> None:
    _upsert("mounts", "path", payload)


def remove_mount_grant(path: str) -> None:
    _remove("mounts", path)


def upsert_bundle_grant(payload: dict[str, Any]) -> None:
    _upsert("bundles", "bundle_id", payload)


def remove_bundle_grant(bundle_id: str) -> None:
    _remove("bundles", bundle_id)


def upsert_public_network_grant(payload: dict[str, Any]) -> None:
    _upsert("public_network", "scope", payload)


def remove_public_network_grant(scope: str) -> None:
    _remove("public_network", scope)


def _state_path() -> Path:
    return state_dir(_STATE_FILE)


def _legacy_state_path() -> Path:
    return state_dir(_LEGACY_STATE_FILE)


def _connect() -> sqlite3.Connection:
    path = native_io_path(_state_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(os.fspath(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sandbox_user_grants ("
        "kind TEXT NOT NULL, "
        "grant_key TEXT NOT NULL, "
        "payload TEXT NOT NULL, "
        "updated_at REAL NOT NULL, "
        "PRIMARY KEY(kind, grant_key)"
        ")"
    )
    _migrate_legacy_json(conn)
    return conn


def _decode_payload(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _upsert_row(
    conn: sqlite3.Connection,
    *,
    kind: str,
    grant_key: str,
    payload: dict[str, Any],
) -> None:
    encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
    conn.execute(
        "INSERT INTO sandbox_user_grants(kind, grant_key, payload, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(kind, grant_key) DO UPDATE SET "
        "payload = excluded.payload, updated_at = excluded.updated_at",
        (kind, grant_key, encoded, time.time()),
    )


def _legacy_key(kind: str, payload: dict[str, Any]) -> str:
    key_field = _KEY_FIELDS[kind]
    if kind == "bundles":
        return str(payload.get(key_field) or payload.get("bundleId") or "").strip()
    return str(payload.get(key_field) or "").strip()


def _migrate_legacy_json(conn: sqlite3.Connection) -> None:
    legacy_path = _legacy_state_path()
    native_legacy_path = native_io_path(legacy_path)
    if not native_legacy_path.exists():
        return
    try:
        parsed = json.loads(native_legacy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(parsed, dict):
        return
    for kind in _KINDS:
        values = parsed.get(kind)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            key = _legacy_key(kind, item)
            if not key:
                continue
            payload = dict(item)
            if kind == "bundles" and "bundle_id" not in payload and "bundleId" in payload:
                payload["bundle_id"] = payload["bundleId"]
            _upsert_row(conn, kind=kind, grant_key=key, payload=payload)
    conn.commit()
    try:
        native_legacy_path.unlink()
    except FileNotFoundError:
        pass


def _upsert(kind: str, key_field: str, payload: dict[str, Any]) -> None:
    key = str(payload.get(key_field) or "").strip()
    if not key:
        return
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _upsert_row(conn, kind=kind, grant_key=key, payload=payload)
        conn.commit()


def _remove(kind: str, key: str) -> None:
    normalized = str(key or "").strip()
    if not normalized:
        return
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM sandbox_user_grants WHERE kind = ? AND grant_key = ?",
            (kind, normalized),
        )
        conn.commit()


__all__ = [
    "load_user_grants_payload",
    "remove_bundle_grant",
    "remove_domain_grant",
    "remove_mount_grant",
    "remove_public_network_grant",
    "upsert_bundle_grant",
    "upsert_domain_grant",
    "upsert_mount_grant",
    "upsert_public_network_grant",
]
