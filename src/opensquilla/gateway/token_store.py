"""Named Token persistence, constant-time verification, and auth throttling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

_TOKEN_PATTERN = re.compile(
    r"\Aosq_(?P<public_id>[a-z0-9-]{4,64})_(?P<secret>[A-Za-z0-9_-]{16,256})\Z"
)
_DUMMY_DIGEST = hashlib.sha256(b"opensquilla-invalid-token").digest()
_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)


@dataclass(frozen=True)
class TokenRecord:
    token_version: int
    public_id: str
    name: str
    secret_digest: bytes
    roles: frozenset[str]
    scopes: frozenset[str]
    capabilities: frozenset[str]
    source_kind: str
    created_at: int
    last_used_at: int | None = field(default=None, compare=False)
    last_peer: str | None = field(default=None, compare=False)
    revoked_at: int | None = None


@dataclass(frozen=True)
class IssuedToken:
    token: str
    record: TokenRecord


def token_public_id(token: object) -> str:
    match = _TOKEN_PATTERN.fullmatch(str(token or ""))
    return match.group("public_id") if match is not None else "legacy"


def _secret_digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _json_set(values: Iterable[str]) -> str:
    return json.dumps(sorted({str(value) for value in values}), separators=(",", ":"))


def _decode_set(raw: object) -> frozenset[str]:
    try:
        values = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(values, list):
        return frozenset()
    return frozenset(str(value) for value in values)


class TokenStore:
    """Small SQLite-backed store sharing the migrated sessions database."""

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sandbox_tokens (
                    public_id TEXT PRIMARY KEY,
                    token_version INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    secret_digest BLOB NOT NULL,
                    roles_json TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER,
                    last_peer TEXT,
                    revoked_at INTEGER
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_sandbox_tokens_active "
                "ON sandbox_tokens(revoked_at, created_at)"
            )

    def create(
        self,
        *,
        name: str,
        roles: Iterable[str],
        scopes: Iterable[str],
        capabilities: Iterable[str],
        source_kind: str = "named",
    ) -> IssuedToken:
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("token name must not be empty")
        public_id = secrets.token_hex(6)
        secret = secrets.token_urlsafe(32)
        digest = _secret_digest(secret)
        created_at = int(time.time())
        record = TokenRecord(
            token_version=1,
            public_id=public_id,
            name=clean_name,
            secret_digest=digest,
            roles=frozenset(str(value) for value in roles),
            scopes=frozenset(str(value) for value in scopes),
            capabilities=frozenset(str(value) for value in capabilities),
            source_kind=str(source_kind),
            created_at=created_at,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sandbox_tokens (
                    public_id, token_version, name, secret_digest, roles_json,
                    scopes_json, capabilities_json, source_kind, created_at,
                    last_used_at, last_peer, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    record.public_id,
                    record.token_version,
                    record.name,
                    record.secret_digest,
                    _json_set(record.roles),
                    _json_set(record.scopes),
                    _json_set(record.capabilities),
                    record.source_kind,
                    record.created_at,
                ),
            )
        return IssuedToken(
            token=f"osq_{public_id}_{secret}",
            record=record,
        )

    def verify(self, token: object, *, peer_ip: str | None = None) -> TokenRecord | None:
        match = _TOKEN_PATTERN.fullmatch(str(token or ""))
        public_id = match.group("public_id") if match is not None else ""
        secret = match.group("secret") if match is not None else ""
        row: sqlite3.Row | None = None
        if public_id:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM sandbox_tokens WHERE public_id = ?",
                    (public_id,),
                ).fetchone()
        expected_digest = bytes(row["secret_digest"]) if row is not None else _DUMMY_DIGEST
        supplied_digest = _secret_digest(secret)
        valid_secret = secrets.compare_digest(expected_digest, supplied_digest)
        if row is None or not valid_secret or row["revoked_at"] is not None:
            return None

        used_at = int(time.time())
        with self._connect() as connection:
            connection.execute(
                "UPDATE sandbox_tokens SET last_used_at = ?, last_peer = ? "
                "WHERE public_id = ? AND revoked_at IS NULL",
                (used_at, peer_ip, public_id),
            )
        return self._record_from_row(row)

    def revoke(self, public_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE sandbox_tokens SET revoked_at = ? "
                "WHERE public_id = ? AND revoked_at IS NULL",
                (int(time.time()), str(public_id)),
            )
        return cursor.rowcount == 1

    def list_active(self) -> tuple[TokenRecord, ...]:
        """List active token metadata without ever loading or returning secrets."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sandbox_tokens WHERE revoked_at IS NULL "
                "ORDER BY created_at DESC, public_id ASC"
            ).fetchall()
        return tuple(self._record_from_row(row) for row in rows)

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> TokenRecord:
        return TokenRecord(
            token_version=int(row["token_version"]),
            public_id=str(row["public_id"]),
            name=str(row["name"]),
            secret_digest=bytes(row["secret_digest"]),
            roles=_decode_set(row["roles_json"]),
            scopes=_decode_set(row["scopes_json"]),
            capabilities=_decode_set(row["capabilities_json"]),
            source_kind=str(row["source_kind"]),
            created_at=int(row["created_at"]),
            last_used_at=(
                int(row["last_used_at"]) if row["last_used_at"] is not None else None
            ),
            last_peer=str(row["last_peer"]) if row["last_peer"] is not None else None,
            revoked_at=int(row["revoked_at"]) if row["revoked_at"] is not None else None,
        )


class AuthFailureLimiter:
    """Sliding-window failure limiter keyed by socket peer and public Token ID."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        window_seconds: float = 60.0,
        burst: int = 5,
    ) -> None:
        self._clock = clock
        self._window_seconds = float(window_seconds)
        self._burst = int(burst)
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def record_failure(self, peer_ip: str | None, public_id: str | None) -> float:
        now = self._clock()
        keys = (
            f"peer:{peer_ip or '<unknown>'}",
            f"token:{public_id or 'legacy'}",
        )
        with self._lock:
            counts: list[int] = []
            for key in keys:
                failures = self._failures[key]
                cutoff = now - self._window_seconds
                while failures and failures[0] <= cutoff:
                    failures.popleft()
                failures.append(now)
                counts.append(len(failures))
        overflow = max(counts) - self._burst
        if overflow <= 0:
            return 0.0
        return _BACKOFF_SECONDS[min(overflow - 1, len(_BACKOFF_SECONDS) - 1)]

    async def wait_after_failure(
        self,
        peer_ip: str | None,
        public_id: str | None,
    ) -> float:
        delay = self.record_failure(peer_ip, public_id)
        if delay > 0:
            await asyncio.sleep(delay)
        return delay


_DEFAULT_AUTH_FAILURE_LIMITER = AuthFailureLimiter()


def default_auth_failure_limiter() -> AuthFailureLimiter:
    return _DEFAULT_AUTH_FAILURE_LIMITER


__all__ = [
    "AuthFailureLimiter",
    "IssuedToken",
    "TokenRecord",
    "TokenStore",
    "default_auth_failure_limiter",
    "token_public_id",
]
