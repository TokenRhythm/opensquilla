"""Durable install operations independent of transport connection lifetimes."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_PROCESS_ID = uuid.uuid4().hex
_CURRENT: contextvars.ContextVar[InstallOperation | None] = contextvars.ContextVar(
    "skill_install_operation",
    default=None,
)


def operation_database(journal_path: Path) -> Path:
    return journal_path.parent / "install-operations.sqlite3"


class InstallOperationStore:
    """Store receipts in the profile's existing Skill transaction state directory."""

    def __init__(self, path: Path, *, root_id: str, process_id: str = _PROCESS_ID) -> None:
        self.path = path
        self.root_id = root_id
        self.process_id = process_id
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or (getattr(path, "is_junction", lambda: False)()):
            raise ValueError("Skill operation database must not be a link")
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS skill_installs (
                root TEXT NOT NULL, owner TEXT NOT NULL, id TEXT NOT NULL,
                signature TEXT NOT NULL, process TEXT NOT NULL, state TEXT NOT NULL,
                phase TEXT NOT NULL, progress TEXT NOT NULL, result TEXT,
                created REAL NOT NULL, updated REAL NOT NULL,
                PRIMARY KEY(root, owner, id))""")
            # Retire IDs independently of expiring receipt contents. An old ID
            # must never become a fresh mutation merely because its receipt aged out.
            db.execute("""CREATE TABLE IF NOT EXISTS skill_install_keys (
                root TEXT NOT NULL, owner TEXT NOT NULL, id TEXT NOT NULL,
                PRIMARY KEY(root, owner, id))""")
            db.execute(
                "INSERT OR IGNORE INTO skill_install_keys "
                "SELECT root, owner, id FROM skill_installs"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, owner: str, operation_id: str, signature: str) -> bool:
        uuid.UUID(operation_id)
        now = time.time()
        with self.connect() as db:
            claimed = db.execute(
                "INSERT OR IGNORE INTO skill_install_keys VALUES (?, ?, ?)",
                (self.root_id, owner, operation_id),
            ).rowcount == 1
            if not claimed and db.execute(
                "SELECT 1 FROM skill_installs WHERE root=? AND owner=? AND id=?",
                (self.root_id, owner, operation_id),
            ).fetchone() is None:
                raise ValueError(
                    "operationId receipt expired; query status without replaying install"
                )
            cursor = db.execute(
                "INSERT OR IGNORE INTO skill_installs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.root_id,
                    owner,
                    operation_id,
                    signature,
                    self.process_id,
                    "running",
                    "resolving",
                    "{}",
                    None,
                    now,
                    now,
                ),
            )
            created = cursor.rowcount == 1
            if not created:
                row = db.execute(
                    "SELECT signature, state FROM skill_installs WHERE root=? AND owner=? AND id=?",
                    (self.root_id, owner, operation_id),
                ).fetchone()
                if row["signature"] != signature and not (
                    row["state"] == "cancelled" and not row["signature"]
                ):
                    raise ValueError("operationId is already associated with another install")
        self.prune()
        return created

    def read(self, owner: str, operation_id: str) -> dict[str, Any]:
        uuid.UUID(operation_id)
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM skill_installs WHERE root=? AND owner=? AND id=?",
                (self.root_id, owner, operation_id),
            ).fetchone()
        if row is None:
            return {
                "operationId": operation_id,
                "state": "unknown",
                "phase": "unknown",
                "terminal": True,
            }
        state = row["state"]
        result = json.loads(row["result"]) if row["result"] else None
        if state == "running" and row["process"] != self.process_id:
            state = "recovery_required"
            result = {
                "success": False,
                "message": "The interrupted install has no proven terminal receipt.",
                "recoveryRequired": True,
            }
        payload = {
            "operationId": operation_id,
            "state": state,
            "phase": row["phase"],
            "terminal": state != "running",
            "progress": json.loads(row["progress"]),
        }
        if result is not None:
            payload["result"] = result
        return payload

    def progress(self, owner: str, operation_id: str, phase: str, progress: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE skill_installs SET phase=?, progress=?, updated=? "
                "WHERE root=? AND owner=? AND id=? AND state='running'",
                (phase, json.dumps(progress), time.time(), self.root_id, owner, operation_id),
            )

    def finish(
        self, owner: str, operation_id: str, result: dict[str, Any], *, state: str = ""
    ) -> None:
        if not state:
            state = (
                "succeeded"
                if result.get("success")
                else "cancelled"
                if result.get("cancelled")
                else "failed"
            )
        with self.connect() as db:
            now = time.time()
            db.execute(
                "INSERT OR IGNORE INTO skill_install_keys VALUES (?, ?, ?)",
                (self.root_id, owner, operation_id),
            )
            db.execute(
                "INSERT OR IGNORE INTO skill_installs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.root_id,
                    owner,
                    operation_id,
                    "",
                    self.process_id,
                    state,
                    "complete",
                    "{}",
                    json.dumps(result),
                    now,
                    now,
                ),
            )
            db.execute(
                "UPDATE skill_installs SET state=?, phase='complete', result=?, updated=? "
                "WHERE root=? AND owner=? AND id=? AND state IN ('running', 'recovery_required')",
                (state, json.dumps(result), time.time(), self.root_id, owner, operation_id),
            )
        self.prune()

    def recover_orphans(self, journal_path: Path) -> None:
        if journal_path.exists():
            return
        result = json.dumps(
            {
                "success": False,
                "recoveryRequired": True,
                "message": "The interrupted installation has no proven terminal receipt.",
            }
        )
        with self.connect() as db:
            db.execute(
                "UPDATE skill_installs SET state='recovery_required', phase='complete', result=?, "
                "updated=? WHERE root=? AND state='running' AND process!=?",
                (result, time.time(), self.root_id, self.process_id),
            )
        self.prune()

    def prune(self) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM skill_installs WHERE root=? AND state!='running' AND updated<?",
                (self.root_id, time.time() - 7 * 86400),
            )
            db.execute(
                "DELETE FROM skill_installs WHERE rowid IN (SELECT rowid FROM skill_installs "
                "WHERE root=? AND state!='running' ORDER BY updated DESC, rowid DESC "
                "LIMIT -1 OFFSET 1000)",
                (self.root_id,),
            )


class InstallOperation:
    def __init__(self, store: InstallOperationStore, owner: str, operation_id: str) -> None:
        self.store = store
        self.owner = owner
        self.id = operation_id
        self.last_progress = 0.0
        self.phase = ""
        self.counts: dict[str, Any] = {}

    def progress(self, phase: str, **counts: Any) -> None:
        self.counts.update(counts)
        now = time.monotonic()
        if phase != self.phase or now - self.last_progress >= 0.25:
            self.store.progress(self.owner, self.id, phase, self.counts)
            self.phase = phase
            self.last_progress = now


def report_install_progress(phase: str, **counts: Any) -> None:
    operation = _CURRENT.get()
    if operation is not None:
        operation.progress(phase, **counts)


def current_install_operation() -> InstallOperation | None:
    return _CURRENT.get()


class InstallOperations:
    def __init__(self, store: InstallOperationStore) -> None:
        self.store = store
        self.tasks: dict[tuple[str, str], asyncio.Task[dict[str, Any]]] = {}

    async def run(
        self,
        owner: str,
        operation_id: str,
        request: dict[str, Any],
        install: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> dict[str, Any]:
        signature = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        key = (owner, operation_id)
        if self.store.create(owner, operation_id, signature):

            async def execute() -> dict[str, Any]:
                context = InstallOperation(self.store, owner, operation_id)
                token = _CURRENT.set(context)
                try:
                    result = dict(await install())
                except asyncio.CancelledError:
                    receipt = self.store.read(owner, operation_id)
                    result = receipt.get("result") or {
                        "success": False,
                        "cancelled": True,
                        "message": "Skill installation cancelled",
                    }
                except Exception as exc:
                    receipt = self.store.read(owner, operation_id)
                    result = receipt.get("result") or {"success": False, "message": str(exc)}
                finally:
                    _CURRENT.reset(token)
                self.store.finish(owner, operation_id, result)
                return result

            task = asyncio.create_task(execute(), name=f"skill-install-{operation_id}")
            self.tasks[key] = task
            task.add_done_callback(lambda done: self.tasks.pop(key, None))
        existing_task = self.tasks.get(key)
        if existing_task is not None:
            try:
                return await asyncio.shield(existing_task)
            except asyncio.CancelledError:
                if not existing_task.cancelled():
                    raise
                cancelled = {
                    "success": False,
                    "cancelled": True,
                    "message": "Skill installation cancelled",
                }
                self.store.finish(owner, operation_id, cancelled)
                return cancelled
        receipt = self.store.read(owner, operation_id)
        if "result" in receipt:
            return dict(receipt["result"])
        raise ValueError("operationId is already running; query install status")

    async def cancel(self, owner: str, operation_id: str) -> dict[str, Any]:
        if self.store.read(owner, operation_id)["state"] == "unknown":
            self.store.create(owner, operation_id, "")
            self.store.finish(
                owner,
                operation_id,
                {
                    "success": False,
                    "cancelled": True,
                    "message": "Skill installation cancelled",
                },
            )
        task = self.tasks.get((owner, operation_id))
        if task is not None:
            task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
                self.store.finish(
                    owner,
                    operation_id,
                    {
                        "success": False,
                        "cancelled": True,
                        "message": "Skill installation cancelled",
                    },
                )
        receipt = self.store.read(owner, operation_id)
        result = receipt.get("result") or {
            "success": False,
            "message": "Install operation is unknown",
        }
        return {**result, "cancelled": bool(result.get("cancelled")), "pending": False}
