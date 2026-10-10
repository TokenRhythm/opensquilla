"""Durable install operations independent of transport connection lifetimes."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any

_PROCESS_ID = uuid.uuid4().hex
_CURRENT: contextvars.ContextVar[InstallOperation | None] = contextvars.ContextVar(
    "skill_install_operation",
    default=None,
)


def operation_database(journal_path: Path) -> Path:
    return journal_path.parent / "install-operations.sqlite3"


async def run_operation_worker[Result](
    function: Callable[..., Result], /, *args: Any, _settling: bool = False, **kwargs: Any,
) -> Result:
    """Drain an owned SQLite worker before delivering caller cancellation."""
    context = contextvars.copy_context()
    operation = asyncio.get_running_loop().run_in_executor(
        None, context.run, partial(function, *args, **kwargs),
    )
    cancellation: asyncio.CancelledError | None = None
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            if cancellation is None:
                raise
            break
    if cancellation is not None and not _settling:
        try:
            operation.result()
        except BaseException:
            pass
        raise cancellation
    return operation.result()


def _interrupted_result(phase: str, result: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    if phase == "rolled_back" and result and result.get("rollbackPerformed") is True:
        return "failed", result
    return "recovery_required", {
        "success": False,
        "message": "The interrupted install has no proven terminal receipt.",
        "recoveryRequired": True,
    }


class InstallOperationStore:
    """Store receipts in the profile's existing Skill transaction state directory."""

    def __init__(
        self, path: Path, *, root_id: str, process_id: str = _PROCESS_ID, initialize: bool = True,
    ) -> None:
        self.path = path
        self.root_id = root_id
        self.process_id = process_id
        self._initialized = False
        self._initialization_lock = threading.Lock()
        if initialize:
            self.initialize()

    def initialize(self) -> None:
        """Prepare schema and retired keys once, on the owning worker."""
        with self._initialization_lock:
            if not self._initialized:
                self._initialize()
                self._initialized = True

    def _initialize(self) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or (getattr(path, "is_junction", lambda: False)()):
            raise ValueError("Skill operation database must not be a link")
        with self._connect() as db:
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
        self.initialize()
        with self._connect() as db:
            yield db

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
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
        return self._receipt(operation_id, row)

    def read_existing(self, owner: str, operation_id: str) -> dict[str, Any]:
        """Observe receipts without creating or maintaining an unleased store."""
        return self._read_existing(owner, operation_id)

    def read_status(self, owner: str, operation_id: str, journal_path: Path) -> dict[str, Any]:
        # Status formerly pruned through the getter only when no journal was
        # retained. Preserve that visibility without deleting durable receipts.
        cutoff = None if journal_path.exists() else time.time() - 7 * 86400
        return self._read_existing(owner, operation_id, cutoff=cutoff)

    def _read_existing(
        self, owner: str, operation_id: str, *, cutoff: float | None = None,
    ) -> dict[str, Any]:
        uuid.UUID(operation_id)
        if not self.path.exists():
            return self._receipt(operation_id, None)
        if self.path.is_symlink() or (getattr(self.path, "is_junction", lambda: False)()):
            raise ValueError("Skill operation database must not be a link")
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            try:
                query = "SELECT * FROM skill_installs WHERE root=? AND owner=? AND id=?"
                params: tuple[Any, ...] = (self.root_id, owner, operation_id)
                if cutoff is not None:
                    query += (
                        " AND (state='running' OR (updated>=? AND rowid IN ("
                        "SELECT rowid FROM skill_installs WHERE root=? AND state!='running' "
                        "AND updated>=? ORDER BY updated DESC, rowid DESC LIMIT 1000)))"
                    )
                    params += (cutoff, self.root_id, cutoff)
                row = db.execute(
                    query, params,
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if str(exc) != "no such table: skill_installs":
                    raise
                row = None
        finally:
            db.close()
        return self._receipt(operation_id, row)

    async def read_async(self, owner: str, operation_id: str) -> dict[str, Any]:
        return await run_operation_worker(self.read_existing, owner, operation_id)

    async def read_status_async(
        self, owner: str, operation_id: str, journal_path: Path,
    ) -> dict[str, Any]:
        return await run_operation_worker(self.read_status, owner, operation_id, journal_path)

    async def finish_async(
        self, owner: str, operation_id: str, result: dict[str, Any], **kwargs: Any,
    ) -> None:
        await run_operation_worker(self.finish, owner, operation_id, result, **kwargs)

    def _receipt(self, operation_id: str, row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {
                "operationId": operation_id,
                "state": "unknown",
                "phase": "unknown",
                "terminal": True,
            }
        state = row["state"]
        result = json.loads(row["result"]) if row["result"] else None
        if state == "running":
            if row["process"] != self.process_id:
                state, result = _interrupted_result(row["phase"], result)
            else:
                # A rollback checkpoint proves the disk state after a crash,
                # but the current worker still owns catalog/cancellation settlement.
                result = None
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

    def checkpoint_rollback(self, owner: str, operation_id: str) -> None:
        """Persist disk/lock restoration before its transaction journal is removed."""
        result = {
            "success": False,
            "message": "Interrupted installation was rolled back.",
            "rollbackPerformed": True,
        }
        with self.connect() as db:
            updated = db.execute(
                "UPDATE skill_installs SET phase='rolled_back', result=?, updated=? "
                "WHERE root=? AND owner=? AND id=? AND process=? AND state='running'",
                (json.dumps(result), time.time(), self.root_id, owner, operation_id,
                 self.process_id),
            ).rowcount
            if updated != 1:
                raise ValueError("Install rollback checkpoint has no running operation")

    def finish(
        self, owner: str, operation_id: str, result: dict[str, Any], *,
        state: str = "", update_details: bool = False,
    ) -> None:
        if not state:
            state = (
                "succeeded"
                if result.get("success")
                else "recovery_required"
                if result.get("recoveryRequired")
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
                "WHERE root=? AND owner=? AND id=? AND (state IN ('running', 'recovery_required') "
                "OR (? AND process=? AND state=?))",
                (state, json.dumps(result), time.time(), self.root_id, owner, operation_id,
                 update_details, self.process_id, state),
            )
        self.prune()

    def recover_orphans(self, journal_path: Path) -> None:
        if journal_path.exists():
            return
        with self.connect() as db:
            rows = db.execute(
                "SELECT owner, id, phase, result FROM skill_installs "
                "WHERE root=? AND state='running' AND process!=?",
                (self.root_id, self.process_id),
            ).fetchall()
            for row in rows:
                state, result = _interrupted_result(
                    row["phase"], json.loads(row["result"]) if row["result"] else None,
                )
                db.execute(
                    "UPDATE skill_installs SET state=?, phase='complete', result=?, updated=? "
                    "WHERE root=? AND owner=? AND id=? AND state='running' AND process!=?",
                    (state, json.dumps(result), time.time(), self.root_id,
                     row["owner"], row["id"], self.process_id),
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
        self._progress_lock = asyncio.Lock()

    def progress(self, phase: str, **counts: Any) -> None:
        self.counts.update(counts)
        now = time.monotonic()
        if phase != self.phase or now - self.last_progress >= 0.25:
            self.store.progress(self.owner, self.id, phase, self.counts)
            self.phase = phase
            self.last_progress = now

    async def progress_async(self, phase: str, **counts: Any) -> None:
        async with self._progress_lock:
            self.counts.update(counts)
            now = time.monotonic()
            if phase != self.phase or now - self.last_progress >= 0.25:
                await run_operation_worker(
                    self.store.progress, self.owner, self.id, phase, dict(self.counts),
                )
                self.phase = phase
                self.last_progress = now


def report_install_progress(phase: str, **counts: Any) -> None:
    operation = _CURRENT.get()
    if operation is not None:
        operation.progress(phase, **counts)


async def report_install_progress_async(phase: str, **counts: Any) -> None:
    operation = _CURRENT.get()
    if operation is not None:
        await operation.progress_async(phase, **counts)


def current_install_operation() -> InstallOperation | None:
    return _CURRENT.get()


class InstallOperations:
    def __init__(self, store: InstallOperationStore) -> None:
        self.store = store
        self.tasks: dict[tuple[str, str], asyncio.Task[dict[str, Any]]] = {}
        self._claim_lock = asyncio.Lock()
        self._closing = False
        self._admissions: set[asyncio.Future[Any]] = set()
        self._started: set[asyncio.Task[Any]] = set()
        self._shutdown_cancelled: set[asyncio.Task[Any]] = set()
        self._settlement_failed = False

    def _own_admission(self, task: asyncio.Task[Any]) -> None:
        self._admissions.add(task)

        def finished(done: asyncio.Task[Any]) -> None:
            self._admissions.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)

    def _settle(
        self, owner: str, operation_id: str, result: dict[str, Any] | None,
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        # One physical worker owns the complete receipt sequence, including
        # global Task cancellation during event-loop shutdown.
        try:
            settled = result
            if settled is None:
                settled = self.store.read_existing(owner, operation_id).get("result") or fallback
            self.store.finish(owner, operation_id, settled, update_details=True)
            return dict(self.store.read_existing(owner, operation_id)["result"])
        except Exception:
            self._settlement_failed = True
            raise

    async def close(self, *, timeout: float | None = None) -> bool:
        """Stop admission and drain owners; false keeps shared resources leased."""
        self._closing = True
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(0.0, timeout)
        while True:
            for key, task in tuple(self.tasks.items()):
                if task.cancelled():
                    settlement = asyncio.create_task(run_operation_worker(
                        self._settle, *key, None, self._cancelled_result(), _settling=True,
                    ))
                    self.tasks[key] = settlement

                    def settled(done: asyncio.Task[dict[str, Any]], key=key) -> None:
                        if not done.cancelled():
                            self._settlement_failed |= done.exception() is not None
                            self.tasks.pop(key, None)

                    settlement.add_done_callback(settled)
            for task in tuple(self.tasks.values()):
                if (task in self._started and not task.done()
                        and task not in self._shutdown_cancelled):
                    self._shutdown_cancelled.add(task)
                    task.cancel()
            pending = {
                task for task in (*self._admissions, *self.tasks.values()) if not task.done()
            }
            if not pending:
                break
            remaining = None if deadline is None else max(0.0, deadline - loop.time())
            if remaining == 0:
                return False
            # wait() does not propagate cancellation to physical owners. A
            # cancelled closer leaves the closed admission fence in place.
            await asyncio.wait(pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        return not self._settlement_failed

    @staticmethod
    def _cancelled_result() -> dict[str, Any]:
        return {"success": False, "cancelled": True, "message": "Skill installation cancelled"}

    async def run(
        self,
        owner: str,
        operation_id: str,
        request: dict[str, Any],
        install: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> dict[str, Any]:
        done = asyncio.get_running_loop().create_future()
        self._admissions.add(done)
        try:
            return await self._run(owner, operation_id, request, install)
        finally:
            self._admissions.discard(done)
            done.set_result(None)

    async def _run(
        self,
        owner: str,
        operation_id: str,
        request: dict[str, Any],
        install: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> dict[str, Any]:
        if self._closing:
            raise RuntimeError("Skill installation service is closing")
        signature = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        key = (owner, operation_id)

        async def execute() -> dict[str, Any]:
            task = asyncio.current_task()
            assert task is not None
            self._started.add(task)
            context = InstallOperation(self.store, owner, operation_id)
            token = _CURRENT.set(context)
            result: dict[str, Any] | None = None
            fallback: dict[str, Any] = {}
            try:
                if self._closing:
                    raise asyncio.CancelledError
                result = dict(await install())
            except asyncio.CancelledError:
                fallback = {
                    "success": False,
                    "cancelled": True,
                    "message": "Skill installation cancelled",
                }
            except Exception as exc:
                fallback = {"success": False, "message": str(exc)}
            finally:
                _CURRENT.reset(token)
            return await run_operation_worker(
                self._settle, owner, operation_id, result, fallback, _settling=True,
            )

        def finished(done: asyncio.Task[dict[str, Any]]) -> None:
            self._started.discard(done)
            self._shutdown_cancelled.discard(done)
            # A Task cancelled before its first instruction has no finally.
            # Keep it owned so close() can persist its terminal receipt.
            if not done.cancelled():
                self._settlement_failed |= done.exception() is not None
                self.tasks.pop(key, None)

        async def claim() -> asyncio.Task[dict[str, Any]] | None:
            # Creation and task registration are one loop-owned admission step.
            # A disconnected waiter cannot strand a durable running receipt.
            async with self._claim_lock:
                if self._closing:
                    raise RuntimeError("Skill installation service is closing")
                if await run_operation_worker(
                    self.store.create, owner, operation_id, signature, _settling=True,
                ):
                    current = asyncio.current_task()
                    if self._closing or (current is not None and current.cancelling()):
                        await run_operation_worker(
                            self._settle, owner, operation_id, None, self._cancelled_result(),
                            _settling=True,
                        )
                        return None
                    task = asyncio.create_task(execute(), name=f"skill-install-{operation_id}")
                    self.tasks[key] = task
                    task.add_done_callback(finished)
                return self.tasks.get(key)

        claim_task = asyncio.create_task(claim(), name=f"skill-install-claim-{operation_id}")
        self._own_admission(claim_task)
        existing_task = await asyncio.shield(claim_task)
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
                await run_operation_worker(self._settle, owner, operation_id, None, cancelled)
                receipt = await self.store.read_async(owner, operation_id)
                return dict(receipt.get("result") or cancelled)
        receipt = await self.store.read_async(owner, operation_id)
        if "result" in receipt:
            return dict(receipt["result"])
        raise ValueError("operationId is already running; query install status")

    async def cancel(self, owner: str, operation_id: str) -> dict[str, Any]:
        if self._closing:
            raise RuntimeError("Skill installation service is closing")
        done = asyncio.get_running_loop().create_future()
        self._admissions.add(done)
        try:
            return await self._cancel(owner, operation_id)
        finally:
            self._admissions.discard(done)
            done.set_result(None)

    async def _cancel(self, owner: str, operation_id: str) -> dict[str, Any]:
        def reserve_cancellation() -> None:
            if self.store.read_existing(owner, operation_id)["state"] == "unknown":
                self.store.create(owner, operation_id, "")
                self._settle(owner, operation_id, None, self._cancelled_result())

        async with self._claim_lock:
            task = self.tasks.get((owner, operation_id))
            if task is not None:
                # Deliver cancellation before yielding to receipt I/O, so a
                # finishing installer still owns its rollback/result settlement.
                task.cancel()
            else:
                await run_operation_worker(reserve_cancellation)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
                await run_operation_worker(
                    self._settle, owner, operation_id, None, self._cancelled_result(),
                )
        receipt = await self.store.read_async(owner, operation_id)
        result = receipt.get("result") or {
            "success": False,
            "message": "Install operation is unknown",
        }
        return {**result, "cancelled": bool(result.get("cancelled")), "pending": False}
