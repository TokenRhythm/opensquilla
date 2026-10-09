"""Transactional persistence for orchestration runs and agent lifecycles."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from opensquilla.compat import aiosqlite
from opensquilla.orchestration.models import (
    ActivationPhase,
    AgentActivationRecord,
    AgentSessionAttachmentRecord,
    AgentSessionRecord,
    DelegatedTaskRecord,
    InboxMessageRecord,
    OrchestrationMode,
    OrchestrationRunRecord,
    RunLifecycle,
    SessionLifecycle,
    TaskBoardStatus,
    TaskOutcome,
)
from opensquilla.orchestration.schema import SCHEMA_STATEMENTS

TransactionFactory = Callable[[str], AbstractAsyncContextManager[Any]]
Clock = Callable[[], int]
IdFactory = Callable[[str], str]


class SessionStorageBinding(Protocol):
    def _write_transaction(
        self,
        operation: str,
        *,
        budget_seconds: float | None = None,
    ) -> AbstractAsyncContextManager[Any]: ...

    def read_transaction(self, operation: str) -> AbstractAsyncContextManager[Any]: ...


class ConcurrentActivationError(RuntimeError):
    """Raised when a session already owns a nonterminal activation."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(raw: Any) -> dict[str, Any]:
    value = json.loads(str(raw))
    if not isinstance(value, dict):
        raise ValueError("stored orchestration payload is not an object")
    return value


def _json_string_set(raw: Any) -> frozenset[str]:
    value = json.loads(str(raw))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("stored orchestration string set is invalid")
    return frozenset(value)


def _json_string_tuple(raw: Any) -> tuple[str, ...]:
    value = json.loads(str(raw))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("stored orchestration string list is invalid")
    return tuple(value)


def _run_from_row(row: Any) -> OrchestrationRunRecord:
    return OrchestrationRunRecord(
        run_id=str(row["run_id"]),
        root_session_id=str(row["root_session_id"]),
        root_task_id=str(row["root_task_id"]),
        mode=OrchestrationMode(str(row["mode"])),
        worker_template_tools=_json_string_set(row["worker_template_tools_json"]),
        lifecycle=RunLifecycle(str(row["lifecycle"])),
        created_at=float(row["created_at"]),
        completed_at=(None if row["completed_at"] is None else float(row["completed_at"])),
        archived_at=None if row["archived_at"] is None else float(row["archived_at"]),
        final_synthesis_completed=bool(row["final_synthesis_completed"]),
    )


def _session_from_row(row: Any) -> AgentSessionRecord:
    return AgentSessionRecord(
        session_id=str(row["session_id"]),
        run_id=str(row["run_id"]),
        profile=str(row["profile"]),
        runtime_session_key=str(row["runtime_session_key"]),
        lifecycle=SessionLifecycle(str(row["lifecycle"])),
        parent_session_id=(
            None if row["parent_session_id"] is None else str(row["parent_session_id"])
        ),
        depth=int(row["depth"]),
        effective_tools=_json_string_set(row["effective_tools_json"]),
        runtime_context=_json_object(row["runtime_context_json"]),
        checkpoint=(
            None if row["checkpoint_json"] is None else _json_object(row["checkpoint_json"])
        ),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        archived_at=None if row["archived_at"] is None else float(row["archived_at"]),
    )


def _task_from_row(row: Any) -> DelegatedTaskRecord:
    return DelegatedTaskRecord(
        task_id=str(row["task_id"]),
        run_id=str(row["run_id"]),
        task_key=str(row["task_key"]),
        owner_session_id=str(row["owner_session_id"]),
        description=str(row["description"]),
        background=bool(row["background"]),
        parent_task_id=(None if row["parent_task_id"] is None else str(row["parent_task_id"])),
        board_status=TaskBoardStatus(str(row["board_status"])),
        acceptance_criteria=(
            None if row["acceptance_criteria"] is None else str(row["acceptance_criteria"])
        ),
        evidence=_json_string_tuple(row["evidence_json"]),
        board_only=bool(row["board_only"]),
        outcome=TaskOutcome(str(row["outcome"])),
        result=None if row["result_json"] is None else _json_object(row["result_json"]),
        retry_of_activation_id=(
            None if row["retry_of_activation_id"] is None else str(row["retry_of_activation_id"])
        ),
        replaces_session_id=(
            None if row["replaces_session_id"] is None else str(row["replaces_session_id"])
        ),
        effective_tools=_json_string_set(row["effective_tools_json"]),
        runtime_context=_json_object(row["runtime_context_json"]),
        created_at=float(row["created_at"]),
        finished_at=None if row["finished_at"] is None else float(row["finished_at"]),
    )


def _activation_from_row(row: Any) -> AgentActivationRecord:
    return AgentActivationRecord(
        activation_id=str(row["activation_id"]),
        session_id=str(row["session_id"]),
        task_id=str(row["task_id"]),
        phase=ActivationPhase(str(row["phase"])),
        route_required=bool(row["route_required"]),
        live_model_call=bool(row["live_model_call"]),
        live_tool_call=bool(row["live_tool_call"]),
        external_wait_id=(
            None if row["external_wait_id"] is None else str(row["external_wait_id"])
        ),
        started_at=None if row["started_at"] is None else float(row["started_at"]),
        finished_at=None if row["finished_at"] is None else float(row["finished_at"]),
        terminal_reason=(None if row["terminal_reason"] is None else str(row["terminal_reason"])),
    )


def _inbox_from_row(row: Any) -> InboxMessageRecord:
    return InboxMessageRecord(
        message_id=str(row["message_id"]),
        session_id=str(row["session_id"]),
        sequence=int(row["sequence"]),
        kind=str(row["kind"]),
        payload=_json_object(row["payload_json"]),
        created_at=float(row["created_at"]),
        acknowledged_at=(None if row["acknowledged_at"] is None else float(row["acknowledged_at"])),
        processed_at=(None if row["processed_at"] is None else float(row["processed_at"])),
        delivery_owner=(None if row["delivery_owner"] is None else str(row["delivery_owner"])),
        delivery_lease_expires_at=(
            None
            if row["delivery_lease_expires_at"] is None
            else float(row["delivery_lease_expires_at"])
        ),
        delivery_attempts=int(row["delivery_attempts"]),
        idempotency_key=(None if row["idempotency_key"] is None else str(row["idempotency_key"])),
    )


def _attachment_from_row(row: Any) -> AgentSessionAttachmentRecord:
    return AgentSessionAttachmentRecord(
        session_id=str(row["session_id"]),
        run_id=str(row["run_id"]),
        parent_session_id=(
            None if row["parent_session_id"] is None else str(row["parent_session_id"])
        ),
        depth=int(row["depth"]),
        attached_at=float(row["attached_at"]),
    )


async def _fetchone(conn: Any, sql: str, params: Sequence[Any] = ()) -> Any | None:
    cursor = await conn.execute(sql, params)
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()


async def _fetchall(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    cursor = await conn.execute(sql, params)
    try:
        return list(await cursor.fetchall())
    finally:
        await cursor.close()


class OrchestrationRepository:
    def __init__(
        self,
        transaction_factory: TransactionFactory,
        *,
        read_transaction_factory: TransactionFactory | None = None,
        clock: Clock = _now_ms,
        id_factory: IdFactory = _new_id,
        owned_connection: Any | None = None,
    ) -> None:
        self._transaction_factory = transaction_factory
        self._read_transaction_factory = read_transaction_factory or transaction_factory
        self._clock = clock
        self._id_factory = id_factory
        self._owned_connection = owned_connection
        self._closed = False

    @classmethod
    async def from_session_storage(
        cls,
        storage: SessionStorageBinding,
        *,
        clock: Clock = _now_ms,
        id_factory: IdFactory = _new_id,
    ) -> OrchestrationRepository:
        def transaction(operation: str) -> AbstractAsyncContextManager[Any]:
            return storage._write_transaction(f"orchestration.{operation}")

        def read_transaction(operation: str) -> AbstractAsyncContextManager[Any]:
            return storage.read_transaction(f"orchestration.{operation}")

        repository = cls(
            transaction,
            read_transaction_factory=read_transaction,
            clock=clock,
            id_factory=id_factory,
        )
        await repository.initialize()
        return repository

    @classmethod
    async def open(
        cls,
        db_path: str | Path = ":memory:",
        *,
        clock: Clock = _now_ms,
        id_factory: IdFactory = _new_id,
    ) -> OrchestrationRepository:
        conn = await aiosqlite.connect(str(db_path), isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        lock = asyncio.Lock()

        @asynccontextmanager
        async def transaction(_operation: str) -> AsyncIterator[Any]:
            async with lock:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    await conn.commit()
                except BaseException:
                    await conn.rollback()
                    raise

        @asynccontextmanager
        async def read_transaction(_operation: str) -> AsyncIterator[Any]:
            async with lock:
                await conn.execute("BEGIN")
                try:
                    yield conn
                finally:
                    if bool(getattr(conn, "in_transaction", False)):
                        await conn.rollback()

        repository = cls(
            transaction,
            read_transaction_factory=read_transaction,
            clock=clock,
            id_factory=id_factory,
            owned_connection=conn,
        )
        try:
            await repository.initialize()
        except BaseException:
            await conn.close()
            raise
        return repository

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owned_connection is not None:
            await self._owned_connection.close()

    def allocate_id(self, prefix: str) -> str:
        return self._id_factory(prefix)

    def _transaction(self, operation: str) -> AbstractAsyncContextManager[Any]:
        if self._closed:
            raise RuntimeError("OrchestrationRepository is closed")
        return self._transaction_factory(operation)

    def _read_transaction(self, operation: str) -> AbstractAsyncContextManager[Any]:
        if self._closed:
            raise RuntimeError("OrchestrationRepository is closed")
        return self._read_transaction_factory(operation)

    async def initialize(self) -> None:
        async with self._transaction("initialize") as conn:
            deferred: list[str] = []
            for statement in SCHEMA_STATEMENTS:
                if "idx_agent_child_result_idempotency" in statement:
                    deferred.append(statement)
                else:
                    await conn.execute(statement)
            await self._ensure_schema_columns(conn)
            for statement in deferred:
                await conn.execute(statement)
            await conn.execute(
                """
                INSERT OR IGNORE INTO agent_session_attachments (
                    session_id, run_id, parent_session_id, depth, attached_at
                )
                SELECT session_id, run_id, parent_session_id, depth, created_at
                FROM agent_sessions
                """
            )

    async def _ensure_schema_columns(self, conn: Any) -> None:
        async def columns(table: str) -> set[str]:
            rows = await _fetchall(conn, f"PRAGMA table_info({table})")
            return {str(row["name"]) for row in rows}

        task_columns = await columns("delegated_tasks")
        if "background" not in task_columns:
            await conn.execute(
                """
                ALTER TABLE delegated_tasks
                ADD COLUMN background INTEGER NOT NULL DEFAULT 0
                    CHECK (background IN (0, 1))
                """
            )
        if "runtime_context_json" not in task_columns:
            await conn.execute(
                """
                ALTER TABLE delegated_tasks
                ADD COLUMN runtime_context_json TEXT NOT NULL DEFAULT '{}'
                """
            )
        if "effective_tools_json" not in task_columns:
            await conn.execute(
                """
                ALTER TABLE delegated_tasks
                ADD COLUMN effective_tools_json TEXT NOT NULL DEFAULT '[]'
                """
            )
        task_additions = {
            "board_status": (
                "TEXT NOT NULL DEFAULT 'planned' CHECK "
                "(board_status IN ('planned', 'working', 'completed', 'blocked'))"
            ),
            "acceptance_criteria": "TEXT",
            "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
            "board_only": "INTEGER NOT NULL DEFAULT 0 CHECK (board_only IN (0, 1))",
        }
        for name, definition in task_additions.items():
            if name not in task_columns:
                await conn.execute(f"ALTER TABLE delegated_tasks ADD COLUMN {name} {definition}")
        activation_columns = await columns("agent_activations")
        if "route_required" not in activation_columns:
            await conn.execute(
                """
                ALTER TABLE agent_activations
                ADD COLUMN route_required INTEGER NOT NULL DEFAULT 0
                    CHECK (route_required IN (0, 1))
                """
            )
        inbox_columns = await columns("agent_inbox_messages")
        inbox_additions = {
            "processed_at": "INTEGER",
            "delivery_owner": "TEXT",
            "delivery_lease_expires_at": "INTEGER",
            "delivery_attempts": "INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0)",
            "idempotency_key": "TEXT",
        }
        for name, definition in inbox_additions.items():
            if name not in inbox_columns:
                await conn.execute(
                    f"ALTER TABLE agent_inbox_messages ADD COLUMN {name} {definition}"
                )

    async def _insert_run(
        self,
        conn: Any,
        run: OrchestrationRunRecord,
        *,
        now: int,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO orchestration_runs (
                run_id, root_session_id, root_task_id, mode,
                worker_template_tools_json, lifecycle,
                final_synthesis_completed, created_at, completed_at, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.root_session_id,
                run.root_task_id,
                run.mode.value,
                _json_dumps(sorted(run.worker_template_tools)),
                run.lifecycle.value,
                int(run.final_synthesis_completed),
                int(run.created_at if run.created_at is not None else now),
                run.completed_at,
                run.archived_at,
            ),
        )

    async def _insert_session(
        self,
        conn: Any,
        session: AgentSessionRecord,
        *,
        now: int,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO agent_sessions (
                session_id, run_id, profile, runtime_session_key,
                lifecycle, parent_session_id,
                depth, effective_tools_json, runtime_context_json,
                checkpoint_json, created_at, updated_at,
                archived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.session_id,
                session.run_id,
                session.profile,
                session.runtime_session_key or session.session_id,
                session.lifecycle.value,
                session.parent_session_id,
                session.depth,
                _json_dumps(sorted(session.effective_tools)),
                _json_dumps(session.runtime_context),
                None if session.checkpoint is None else _json_dumps(session.checkpoint),
                int(session.created_at if session.created_at is not None else now),
                int(session.updated_at if session.updated_at is not None else now),
                session.archived_at,
            ),
        )

    async def _insert_attachment(
        self,
        conn: Any,
        *,
        session_id: str,
        run_id: str,
        parent_session_id: str | None,
        depth: int,
        now: int,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO agent_session_attachments (
                session_id, run_id, parent_session_id, depth, attached_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, run_id, parent_session_id, depth, now),
        )

    async def _insert_task(
        self,
        conn: Any,
        task: DelegatedTaskRecord,
        *,
        now: int,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO delegated_tasks (
                task_id, run_id, parent_task_id, task_key, owner_session_id,
                description, background, board_status, acceptance_criteria,
                evidence_json, board_only, outcome, result_json,
                retry_of_activation_id, replaces_session_id, effective_tools_json,
                runtime_context_json, created_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.task_id,
                task.run_id,
                task.parent_task_id,
                task.task_key,
                task.owner_session_id,
                task.description,
                int(task.background),
                task.board_status.value,
                task.acceptance_criteria,
                _json_dumps(list(task.evidence)),
                int(task.board_only),
                task.outcome.value,
                None if task.result is None else _json_dumps(task.result),
                task.retry_of_activation_id,
                task.replaces_session_id,
                _json_dumps(sorted(task.effective_tools)),
                _json_dumps(task.runtime_context),
                int(task.created_at if task.created_at is not None else now),
                task.finished_at,
            ),
        )

    async def _insert_activation(
        self,
        conn: Any,
        activation: AgentActivationRecord,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO agent_activations (
                activation_id, session_id, task_id, phase,
                live_model_call, live_tool_call, external_wait_id,
                started_at, finished_at, terminal_reason, route_required
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                activation.activation_id,
                activation.session_id,
                activation.task_id,
                activation.phase.value,
                int(activation.live_model_call),
                int(activation.live_tool_call),
                activation.external_wait_id,
                activation.started_at,
                activation.finished_at,
                activation.terminal_reason,
                int(activation.route_required),
            ),
        )

    async def _enqueue_message_in_transaction(
        self,
        conn: Any,
        *,
        session_id: str,
        kind: str,
        payload: Mapping[str, Any],
        now: int,
        idempotency_key: str | None = None,
    ) -> InboxMessageRecord:
        row = await _fetchone(
            conn,
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence
            FROM agent_inbox_messages
            WHERE session_id = ?
            """,
            (session_id,),
        )
        assert row is not None
        sequence = int(row["sequence"])
        message_id = self.allocate_id("message")
        await conn.execute(
            """
            INSERT INTO agent_inbox_messages (
                message_id, session_id, sequence, kind, payload_json, created_at,
                idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                session_id,
                sequence,
                kind,
                _json_dumps(payload),
                now,
                idempotency_key,
            ),
        )
        return InboxMessageRecord(
            message_id=message_id,
            session_id=session_id,
            sequence=sequence,
            kind=kind,
            payload=dict(payload),
            created_at=now,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    async def _assert_parent_can_delegate_in_transaction(
        conn: Any,
        *,
        parent_session_id: str,
        require_live_parent: bool,
        expected_activation_id: str | None,
    ) -> None:
        if not require_live_parent:
            return
        row = await _fetchone(
            conn,
            """
            SELECT * FROM agent_activations
            WHERE session_id = ? AND phase != 'released'
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (parent_session_id,),
        )
        if row is None:
            raise ConcurrentActivationError("parent activation is no longer live")
        if expected_activation_id is not None and str(row["activation_id"]) != (
            expected_activation_id
        ):
            raise ConcurrentActivationError("parent activation generation changed")
        if str(row["phase"]) == ActivationPhase.STOPPING.value:
            raise ConcurrentActivationError("parent activation is stopping")

    async def create_run(self, run: OrchestrationRunRecord) -> OrchestrationRunRecord:
        now = self._clock()
        async with self._transaction("create_run") as conn:
            await self._insert_run(conn, run, now=now)
        return run

    async def create_run_bundle(
        self,
        *,
        run: OrchestrationRunRecord,
        root_session: AgentSessionRecord,
        root_task: DelegatedTaskRecord,
    ) -> OrchestrationRunRecord:
        """Atomically create a run and its root durable identities."""

        now = self._clock()
        async with self._transaction("create_run_bundle") as conn:
            await self._insert_run(conn, run, now=now)
            await self._insert_session(conn, root_session, now=now)
            await self._insert_attachment(
                conn,
                session_id=root_session.session_id,
                run_id=run.run_id,
                parent_session_id=None,
                depth=0,
                now=now,
            )
            await self._insert_task(conn, root_task, now=now)
        return run

    async def repair_run_bundle(
        self,
        *,
        run: OrchestrationRunRecord,
        root_session: AgentSessionRecord,
        root_task: DelegatedTaskRecord,
    ) -> None:
        """Repair root records left by the pre-bundle creation path atomically."""

        now = self._clock()
        async with self._transaction("repair_run_bundle") as conn:
            run_row = await _fetchone(
                conn,
                "SELECT * FROM orchestration_runs WHERE run_id = ?",
                (run.run_id,),
            )
            if run_row is None:
                raise ValueError(f"orchestration run not found: {run.run_id}")
            stored_run = _run_from_row(run_row)
            if (
                stored_run.root_session_id != run.root_session_id
                or stored_run.root_task_id != run.root_task_id
                or stored_run.mode is not run.mode
                or stored_run.worker_template_tools != run.worker_template_tools
            ):
                raise ValueError("orchestration run configuration changed")

            session_row = await _fetchone(
                conn,
                "SELECT * FROM agent_sessions WHERE session_id = ?",
                (root_session.session_id,),
            )
            if session_row is None:
                await self._insert_session(conn, root_session, now=now)
            else:
                stored_session = _session_from_row(session_row)
                if (
                    stored_session.run_id != run.run_id
                    or stored_session.parent_session_id is not None
                    or stored_session.depth != 0
                ):
                    raise ValueError("orchestration root session identity changed")

            attachment_row = await _fetchone(
                conn,
                """
                SELECT * FROM agent_session_attachments
                WHERE session_id = ? AND run_id = ?
                """,
                (root_session.session_id, run.run_id),
            )
            if attachment_row is None:
                await self._insert_attachment(
                    conn,
                    session_id=root_session.session_id,
                    run_id=run.run_id,
                    parent_session_id=None,
                    depth=0,
                    now=now,
                )
            else:
                attachment = _attachment_from_row(attachment_row)
                if attachment.parent_session_id is not None or attachment.depth != 0:
                    raise ValueError("orchestration root attachment identity changed")

            task_row = await _fetchone(
                conn,
                "SELECT * FROM delegated_tasks WHERE task_id = ?",
                (root_task.task_id,),
            )
            if task_row is None:
                await self._insert_task(conn, root_task, now=now)
            else:
                stored_task = _task_from_row(task_row)
                if (
                    stored_task.run_id != run.run_id
                    or stored_task.owner_session_id != root_session.session_id
                    or stored_task.parent_task_id is not None
                    or stored_task.task_key != root_task.task_key
                ):
                    raise ValueError("orchestration root task identity changed")

    async def get_run(self, run_id: str) -> OrchestrationRunRecord | None:
        async with self._read_transaction("get_run") as conn:
            row = await _fetchone(
                conn,
                "SELECT * FROM orchestration_runs WHERE run_id = ?",
                (run_id,),
            )
        return None if row is None else _run_from_row(row)

    async def get_run_by_root_task_id(
        self,
        root_task_id: str,
    ) -> OrchestrationRunRecord | None:
        async with self._read_transaction("get_run_by_root_task_id") as conn:
            row = await _fetchone(
                conn,
                "SELECT * FROM orchestration_runs WHERE root_task_id = ?",
                (root_task_id,),
            )
        return None if row is None else _run_from_row(row)

    async def create_session(self, session: AgentSessionRecord) -> AgentSessionRecord:
        now = self._clock()
        async with self._transaction("create_session") as conn:
            await self._insert_session(conn, session, now=now)
            await self._insert_attachment(
                conn,
                session_id=session.session_id,
                run_id=session.run_id,
                parent_session_id=session.parent_session_id,
                depth=session.depth,
                now=now,
            )
        return session

    async def get_session_attachment(
        self,
        *,
        run_id: str,
        session_id: str,
    ) -> AgentSessionAttachmentRecord | None:
        async with self._read_transaction("get_session_attachment") as conn:
            row = await _fetchone(
                conn,
                """
                SELECT * FROM agent_session_attachments
                WHERE run_id = ? AND session_id = ?
                """,
                (run_id, session_id),
            )
        return None if row is None else _attachment_from_row(row)

    async def attach_session(
        self,
        *,
        run_id: str,
        session_id: str,
        parent_session_id: str | None,
        depth: int,
    ) -> AgentSessionAttachmentRecord:
        now = self._clock()
        async with self._transaction("attach_session") as conn:
            await self._insert_attachment(
                conn,
                session_id=session_id,
                run_id=run_id,
                parent_session_id=parent_session_id,
                depth=depth,
                now=now,
            )
        attachment = await self.get_session_attachment(run_id=run_id, session_id=session_id)
        if attachment is None:
            raise RuntimeError("session attachment disappeared after creation")
        return attachment

    async def get_session(self, session_id: str) -> AgentSessionRecord | None:
        async with self._read_transaction("get_session") as conn:
            row = await _fetchone(
                conn,
                "SELECT * FROM agent_sessions WHERE session_id = ?",
                (session_id,),
            )
        return None if row is None else _session_from_row(row)

    async def session_is_in_subtree(
        self,
        *,
        ancestor_session_id: str,
        descendant_session_id: str,
        run_id: str | None = None,
    ) -> bool:
        run_filter = "" if run_id is None else " AND run_id = ?"
        async with self._read_transaction("session_is_in_subtree") as conn:
            row = await _fetchone(
                conn,
                """
                WITH RECURSIVE subtree(session_id) AS (
                    SELECT session_id
                    FROM agent_session_attachments
                    WHERE session_id = ?
                    """
                + run_filter
                + """
                    UNION ALL
                    SELECT child.session_id
                    FROM agent_session_attachments AS child
                    JOIN subtree AS parent
                      ON child.parent_session_id = parent.session_id
                    """
                + run_filter
                + """
                )
                SELECT 1 AS found
                FROM subtree
                WHERE session_id = ?
                LIMIT 1
                """,
                (
                    (ancestor_session_id, descendant_session_id)
                    if run_id is None
                    else (ancestor_session_id, run_id, run_id, descendant_session_id)
                ),
            )
        return row is not None

    async def create_task(self, task: DelegatedTaskRecord) -> DelegatedTaskRecord:
        now = self._clock()
        try:
            async with self._transaction("create_task") as conn:
                await self._insert_task(conn, task, now=now)
        except aiosqlite.IntegrityError as exc:
            if "delegated_tasks.run_id, delegated_tasks.task_key" in str(exc):
                raise ValueError("task key already exists in orchestration run") from exc
            raise
        return task

    async def get_task(self, task_id: str) -> DelegatedTaskRecord | None:
        async with self._read_transaction("get_task") as conn:
            row = await _fetchone(
                conn,
                "SELECT * FROM delegated_tasks WHERE task_id = ?",
                (task_id,),
            )
        return None if row is None else _task_from_row(row)

    async def find_task_by_key(
        self,
        run_id: str,
        task_key: str,
    ) -> DelegatedTaskRecord | None:
        async with self._read_transaction("find_task_by_key") as conn:
            row = await _fetchone(
                conn,
                "SELECT * FROM delegated_tasks WHERE run_id = ? AND task_key = ?",
                (run_id, task_key),
            )
        return None if row is None else _task_from_row(row)

    async def list_task_board(self, run_id: str) -> list[DelegatedTaskRecord]:
        async with self._read_transaction("list_task_board") as conn:
            rows = await _fetchall(
                conn,
                "SELECT * FROM delegated_tasks WHERE run_id = ? ORDER BY rowid ASC",
                (run_id,),
            )
        return [_task_from_row(row) for row in rows]

    async def list_session_recall_tasks(
        self,
        *,
        parent_runtime_session_key: str,
        profile: str,
        limit: int = 100,
    ) -> list[DelegatedTaskRecord]:
        """Return useful task history for same-profile children of one persistent parent."""

        if limit < 1:
            return []
        async with self._read_transaction("list_session_recall_tasks") as conn:
            rows = await _fetchall(
                conn,
                """
                SELECT task.*
                FROM delegated_tasks AS task
                JOIN agent_sessions AS child ON child.session_id = task.owner_session_id
                JOIN agent_sessions AS parent ON parent.session_id = child.parent_session_id
                WHERE parent.runtime_session_key = ?
                  AND child.profile = ?
                  AND child.lifecycle != 'archived'
                  AND task.board_only = 0
                  AND task.outcome = 'succeeded'
                ORDER BY task.created_at DESC, task.rowid DESC
                LIMIT ?
                """,
                (parent_runtime_session_key, profile, limit),
            )
        return [_task_from_row(row) for row in rows]

    async def list_prior_unsynthesized_child_work(
        self,
        *,
        root_runtime_session_key: str,
        exclude_run_id: str,
        limit: int = 20,
    ) -> list[tuple[DelegatedTaskRecord, str]]:
        """Return recent child work hidden by an interrupted root turn."""

        if limit < 1:
            return []
        async with self._read_transaction("list_prior_unsynthesized_child_work") as conn:
            rows = await _fetchall(
                conn,
                """
                SELECT task.*, owner.session_id AS child_session_id
                FROM orchestration_runs AS run
                JOIN agent_sessions AS root ON root.session_id = run.root_session_id
                JOIN delegated_tasks AS task ON task.run_id = run.run_id
                JOIN agent_sessions AS owner ON owner.session_id = task.owner_session_id
                WHERE root.runtime_session_key = ?
                  AND run.run_id != ?
                  AND run.final_synthesis_completed = 0
                  AND task.task_id != run.root_task_id
                  AND task.board_only = 0
                ORDER BY run.created_at DESC, task.created_at DESC
                LIMIT ?
                """,
                (root_runtime_session_key, exclude_run_id, limit),
            )
        return [
            (_task_from_row(row), str(row["child_session_id"]))
            for row in reversed(rows)
        ]

    async def list_single_agent_results(
        self, *, root_runtime_session_key: str
    ) -> list[DelegatedTaskRecord]:
        """Return completed child results for one root chat without exposing other runs."""

        async with self._read_transaction("list_single_agent_results") as conn:
            rows = await _fetchall(
                conn,
                """
                SELECT task.*, root_task.runtime_context_json AS root_context_json
                FROM orchestration_runs AS run
                JOIN agent_sessions AS root ON root.session_id = run.root_session_id
                JOIN delegated_tasks AS root_task ON root_task.task_id = run.root_task_id
                JOIN delegated_tasks AS task ON task.run_id = run.run_id
                WHERE root.runtime_session_key = ?
                  AND task.parent_task_id = run.root_task_id
                  AND task.board_only = 0
                  AND task.result_json IS NOT NULL
                ORDER BY task.created_at ASC, task.rowid ASC
                """,
                (root_runtime_session_key,),
            )
        return [
            _task_from_row(row)
            for row in rows
            if _json_object(row["root_context_json"]).get("single_agent_mode")
        ]

    async def update_task_board_item(
        self,
        task_id: str,
        *,
        status: TaskBoardStatus,
        evidence: str | None = None,
        reopen: bool = False,
    ) -> DelegatedTaskRecord:
        now = self._clock()
        async with self._transaction("update_task_board_item") as conn:
            row = await _fetchone(
                conn,
                "SELECT * FROM delegated_tasks WHERE task_id = ?",
                (task_id,),
            )
            if row is None:
                raise ValueError(f"task not found: {task_id}")
            current_evidence = list(_json_string_tuple(row["evidence_json"]))
            if evidence and evidence not in current_evidence:
                current_evidence.append(evidence)
            outcome = str(row["outcome"])
            result_json = row["result_json"]
            finished_at = row["finished_at"]
            if reopen:
                if status is not TaskBoardStatus.PLANNED:
                    raise ValueError("reopened task must return to planned")
                if outcome == TaskOutcome.PENDING.value:
                    raise ValueError("task is already open")
                if bool(row["board_only"]):
                    outcome = TaskOutcome.PENDING.value
                    result_json = None
                else:
                    outcome = TaskOutcome.FAILED.value
                    result_json = _json_dumps(
                        {
                            "summary": "task reopened",
                            "error": "task reopened",
                            "retry_same_agent": True,
                        }
                    )
                finished_at = None
            elif bool(row["board_only"]) and status in {
                TaskBoardStatus.COMPLETED,
                TaskBoardStatus.BLOCKED,
            }:
                outcome = (
                    TaskOutcome.SUCCEEDED.value
                    if status is TaskBoardStatus.COMPLETED
                    else TaskOutcome.FAILED.value
                )
                result_json = _json_dumps({"summary": evidence or status.value, "board_only": True})
                finished_at = now
            await conn.execute(
                """
                UPDATE delegated_tasks
                SET board_status = ?, evidence_json = ?, outcome = ?,
                    result_json = ?, finished_at = ?
                WHERE task_id = ?
                """,
                (
                    status.value,
                    _json_dumps(current_evidence),
                    outcome,
                    result_json,
                    finished_at,
                    task_id,
                ),
            )
            updated = await _fetchone(
                conn,
                "SELECT * FROM delegated_tasks WHERE task_id = ?",
                (task_id,),
            )
        assert updated is not None
        return _task_from_row(updated)

    async def list_pending_tasks_for_session(
        self,
        session_id: str,
        *,
        exclude_task_id: str | None = None,
    ) -> list[DelegatedTaskRecord]:
        params: list[Any] = [session_id]
        exclusion = ""
        if exclude_task_id is not None:
            exclusion = " AND task_id != ?"
            params.append(exclude_task_id)
        async with self._read_transaction("list_pending_tasks_for_session") as conn:
            rows = await _fetchall(
                conn,
                """
                SELECT * FROM delegated_tasks
                WHERE owner_session_id = ? AND outcome = 'pending' AND board_only = 0
                """
                + exclusion
                + " ORDER BY rowid ASC",
                params,
            )
        return [_task_from_row(row) for row in rows]

    async def delegated_work_state(self, run_id: str) -> tuple[int, int]:
        """Return non-root delegated task count and the pending subset."""

        async with self._read_transaction("delegated_work_state") as conn:
            row = await _fetchone(
                conn,
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN task.outcome = 'pending' THEN 1 ELSE 0 END) AS pending
                FROM delegated_tasks AS task
                JOIN orchestration_runs AS run ON run.run_id = task.run_id
                WHERE task.run_id = ? AND task.task_id != run.root_task_id
                  AND task.board_only = 0
                """,
                (run_id,),
            )
        assert row is not None
        return int(row["total"] or 0), int(row["pending"] or 0)

    async def ancestor_task_keys(self, task_id: str) -> tuple[str, ...]:
        async with self._read_transaction("ancestor_task_keys") as conn:
            rows = await _fetchall(
                conn,
                """
                WITH RECURSIVE ancestors(task_id, task_key, parent_task_id) AS (
                    SELECT task_id, task_key, parent_task_id
                    FROM delegated_tasks
                    WHERE task_id = ?
                    UNION ALL
                    SELECT parent.task_id, parent.task_key, parent.parent_task_id
                    FROM delegated_tasks AS parent
                    JOIN ancestors AS child ON child.parent_task_id = parent.task_id
                )
                SELECT task_key FROM ancestors
                """,
                (task_id,),
            )
        return tuple(str(row["task_key"]) for row in rows)

    async def finish_task(
        self,
        task_id: str,
        *,
        outcome: TaskOutcome,
        result: Mapping[str, Any],
    ) -> None:
        if outcome is TaskOutcome.PENDING:
            raise ValueError("terminal task outcome is required")
        now = self._clock()
        async with self._transaction("finish_task") as conn:
            cursor = await conn.execute(
                """
                UPDATE delegated_tasks
                SET outcome = ?, result_json = ?, finished_at = ?
                WHERE task_id = ? AND outcome = 'pending'
                """,
                (outcome.value, _json_dumps(result), now, task_id),
            )
            try:
                changed = cursor.rowcount
            finally:
                await cursor.close()
            if changed != 1:
                raise ValueError(f"pending task not found: {task_id}")

    async def promote_pending_task_to_foreground(
        self,
        task_id: str,
    ) -> DelegatedTaskRecord:
        """Durably suppress background delivery when a foreground caller attaches."""

        async with self._transaction("promote_pending_task_to_foreground") as conn:
            await conn.execute(
                """
                UPDATE delegated_tasks
                SET background = 0
                WHERE task_id = ? AND outcome = 'pending'
                """,
                (task_id,),
            )
            row = await _fetchone(
                conn,
                "SELECT * FROM delegated_tasks WHERE task_id = ?",
                (task_id,),
            )
        if row is None:
            raise ValueError(f"task not found: {task_id}")
        return _task_from_row(row)

    async def complete_task(self, task_id: str, *, result: Mapping[str, Any]) -> None:
        await self.finish_task(
            task_id,
            outcome=TaskOutcome.SUCCEEDED,
            result=result,
        )

    async def retry_task_bundle(
        self,
        *,
        task_id: str,
        session: AgentSessionRecord | None,
        session_is_new: bool,
        description: str,
        parent_task_id: str,
        retry_of_activation_id: str,
        replaces_session_id: str | None,
        background: bool,
        effective_tools: frozenset[str],
        runtime_context: Mapping[str, Any],
        activation: AgentActivationRecord,
        run_id: str,
        parent_session_id: str,
        parent_activation_id: str | None = None,
        require_live_parent: bool = False,
        depth: int,
        max_direct_children: int,
    ) -> tuple[DelegatedTaskRecord, AgentActivationRecord]:
        """Atomically retry work, optionally replacing its persistent session."""

        if session is None:
            raise ValueError("retry session not found")
        now = self._clock()
        async with self._transaction("retry_task_bundle") as conn:
            await self._assert_parent_can_delegate_in_transaction(
                conn,
                parent_session_id=parent_session_id,
                require_live_parent=require_live_parent,
                expected_activation_id=parent_activation_id,
            )
            await conn.execute(
                """
                UPDATE agent_inbox_messages
                SET acknowledged_at = COALESCE(acknowledged_at, ?),
                    processed_at = COALESCE(processed_at, ?),
                    delivery_owner = NULL, delivery_lease_expires_at = NULL
                WHERE kind = 'child_result'
                  AND json_extract(payload_json, '$.task_id') = ?
                  AND json_extract(payload_json, '$.activation_id') = ?
                """,
                (now, now, task_id, retry_of_activation_id),
            )
            if session_is_new:
                await self._insert_session(conn, session, now=now)
                await self._insert_attachment(
                    conn,
                    session_id=session.session_id,
                    run_id=run_id,
                    parent_session_id=parent_session_id,
                    depth=depth,
                    now=now,
                )
            cursor = await conn.execute(
                """
                UPDATE delegated_tasks
                SET owner_session_id = ?, description = ?, parent_task_id = ?,
                    background = ?, board_status = 'planned', outcome = 'pending',
                    result_json = NULL,
                    finished_at = NULL, retry_of_activation_id = ?,
                    replaces_session_id = ?, effective_tools_json = ?,
                    runtime_context_json = ?
                WHERE task_id = ?
                  AND run_id = ?
                  AND outcome IN ('failed', 'interrupted', 'timed_out')
                """,
                (
                    session.session_id,
                    description,
                    parent_task_id,
                    int(background),
                    retry_of_activation_id,
                    replaces_session_id,
                    _json_dumps(sorted(effective_tools)),
                    _json_dumps(runtime_context),
                    task_id,
                    run_id,
                ),
            )
            try:
                if cursor.rowcount != 1:
                    raise ValueError(f"terminal retryable task not found: {task_id}")
            finally:
                await cursor.close()
            count_row = await _fetchone(
                conn,
                """
                SELECT COUNT(*) AS count
                FROM agent_session_attachments AS child
                JOIN agent_activations AS active ON active.session_id = child.session_id
                JOIN delegated_tasks AS active_task ON active_task.task_id = active.task_id
                WHERE child.run_id = ?
                  AND child.parent_session_id = ?
                  AND active_task.run_id = ?
                  AND active.phase IN ('starting', 'running', 'stopping')
                """,
                (run_id, parent_session_id, run_id),
            )
            assert count_row is not None
            activation.phase = (
                ActivationPhase.STARTING
                if int(count_row["count"]) < max_direct_children
                else ActivationPhase.QUEUED
            )
            await self._insert_activation(conn, activation)
            await conn.execute(
                """
                UPDATE agent_sessions
                SET lifecycle = 'active', archived_at = NULL, updated_at = ?
                WHERE session_id = ?
                """,
                (now, session.session_id),
            )
            task_row = await _fetchone(
                conn,
                "SELECT * FROM delegated_tasks WHERE task_id = ?",
                (task_id,),
            )
        if task_row is None:
            raise RuntimeError("retried task disappeared")
        return _task_from_row(task_row), activation

    async def create_activation(
        self,
        activation: AgentActivationRecord,
    ) -> AgentActivationRecord:
        try:
            async with self._transaction("create_activation") as conn:
                await self._insert_activation(conn, activation)
        except aiosqlite.IntegrityError as exc:
            if "agent_activations.session_id" in str(exc):
                raise ConcurrentActivationError(
                    f"session already has a nonterminal activation: {activation.session_id}"
                ) from exc
            raise
        return activation

    async def create_delegation_bundle(
        self,
        *,
        session: AgentSessionRecord,
        task: DelegatedTaskRecord,
        activation: AgentActivationRecord,
        max_direct_children: int,
        parent_activation_id: str | None = None,
        require_live_parent: bool = False,
    ) -> AgentActivationRecord:
        """Atomically persist a new child session, task, and execution slot."""

        if max_direct_children < 1:
            raise ValueError("max_direct_children must be positive")
        now = self._clock()
        async with self._transaction("create_delegation_bundle") as conn:
            await self._assert_parent_can_delegate_in_transaction(
                conn,
                parent_session_id=str(session.parent_session_id or ""),
                require_live_parent=require_live_parent,
                expected_activation_id=parent_activation_id,
            )
            await self._insert_session(conn, session, now=now)
            await self._insert_attachment(
                conn,
                session_id=session.session_id,
                run_id=task.run_id,
                parent_session_id=session.parent_session_id,
                depth=session.depth,
                now=now,
            )
            await self._insert_task(conn, task, now=now)
            row = await _fetchone(
                conn,
                """
                SELECT COUNT(*) AS count
                FROM agent_session_attachments AS child
                JOIN agent_activations AS active ON active.session_id = child.session_id
                JOIN delegated_tasks AS active_task ON active_task.task_id = active.task_id
                WHERE child.run_id = ?
                  AND child.parent_session_id = ?
                  AND active_task.run_id = ?
                  AND active.phase IN ('starting', 'running', 'stopping')
                """,
                (task.run_id, session.parent_session_id, task.run_id),
            )
            assert row is not None
            activation.phase = (
                ActivationPhase.STARTING
                if int(row["count"]) < max_direct_children
                else ActivationPhase.QUEUED
            )
            await self._insert_activation(conn, activation)
        return activation

    async def activate_board_task_bundle(
        self,
        *,
        session: AgentSessionRecord,
        task_id: str,
        description: str,
        acceptance_criteria: str,
        background: bool,
        effective_tools: frozenset[str],
        runtime_context: Mapping[str, Any],
        activation: AgentActivationRecord,
        max_direct_children: int,
        parent_activation_id: str | None = None,
        require_live_parent: bool = False,
    ) -> tuple[DelegatedTaskRecord, AgentActivationRecord]:
        """Assign one pending board item to a newly created child session."""

        if max_direct_children < 1:
            raise ValueError("max_direct_children must be positive")
        now = self._clock()
        async with self._transaction("activate_board_task_bundle") as conn:
            await self._assert_parent_can_delegate_in_transaction(
                conn,
                parent_session_id=str(session.parent_session_id or ""),
                require_live_parent=require_live_parent,
                expected_activation_id=parent_activation_id,
            )
            board_task = await _fetchone(
                conn,
                "SELECT * FROM delegated_tasks WHERE task_id = ?",
                (task_id,),
            )
            if (
                board_task is None
                or not bool(board_task["board_only"])
                or str(board_task["outcome"]) != TaskOutcome.PENDING.value
                or str(board_task["board_status"])
                not in {
                    TaskBoardStatus.PLANNED.value,
                    TaskBoardStatus.WORKING.value,
                }
                or str(board_task["owner_session_id"]) != str(session.parent_session_id or "")
            ):
                raise ValueError("pending task-board item is no longer assignable")
            await self._insert_session(conn, session, now=now)
            await self._insert_attachment(
                conn,
                session_id=session.session_id,
                run_id=str(board_task["run_id"]),
                parent_session_id=session.parent_session_id,
                depth=session.depth,
                now=now,
            )
            await conn.execute(
                """
                UPDATE delegated_tasks
                SET owner_session_id = ?, description = ?, acceptance_criteria = ?,
                    background = ?, board_only = 0, effective_tools_json = ?,
                    runtime_context_json = ?
                WHERE task_id = ?
                """,
                (
                    session.session_id,
                    description,
                    acceptance_criteria,
                    int(background),
                    _json_dumps(sorted(effective_tools)),
                    _json_dumps(runtime_context),
                    task_id,
                ),
            )
            count_row = await _fetchone(
                conn,
                """
                SELECT COUNT(*) AS count
                FROM agent_session_attachments AS child
                JOIN agent_activations AS active ON active.session_id = child.session_id
                JOIN delegated_tasks AS active_task ON active_task.task_id = active.task_id
                WHERE child.run_id = ?
                  AND child.parent_session_id = ?
                  AND active_task.run_id = ?
                  AND active.phase IN ('starting', 'running', 'stopping')
                """,
                (
                    str(board_task["run_id"]),
                    session.parent_session_id,
                    str(board_task["run_id"]),
                ),
            )
            assert count_row is not None
            activation.phase = (
                ActivationPhase.STARTING
                if int(count_row["count"]) < max_direct_children
                else ActivationPhase.QUEUED
            )
            await self._insert_activation(conn, activation)
            updated = await _fetchone(
                conn,
                "SELECT * FROM delegated_tasks WHERE task_id = ?",
                (task_id,),
            )
        assert updated is not None
        return _task_from_row(updated), activation

    async def continue_session_bundle(
        self,
        *,
        session: AgentSessionRecord,
        task: DelegatedTaskRecord,
        activation: AgentActivationRecord,
        parent_session_id: str,
        depth: int,
        max_direct_children: int,
        parent_activation_id: str | None = None,
        require_live_parent: bool = False,
        planned_task_id: str | None = None,
    ) -> tuple[AgentActivationRecord, bool]:
        """Atomically queue work on a live session or cold-restore one.

        The boolean result is true when the task was queued behind an existing
        activation and false when a new activation was created for it.
        """

        if max_direct_children < 1:
            raise ValueError("max_direct_children must be positive")
        now = self._clock()
        async with self._transaction("continue_session_bundle") as conn:
            await self._assert_parent_can_delegate_in_transaction(
                conn,
                parent_session_id=parent_session_id,
                require_live_parent=require_live_parent,
                expected_activation_id=parent_activation_id,
            )
            attachment = await _fetchone(
                conn,
                """
                SELECT * FROM agent_session_attachments
                WHERE run_id = ? AND session_id = ?
                """,
                (task.run_id, session.session_id),
            )
            if attachment is None:
                await self._insert_attachment(
                    conn,
                    session_id=session.session_id,
                    run_id=task.run_id,
                    parent_session_id=parent_session_id,
                    depth=depth,
                    now=now,
                )
            current = await _fetchone(
                conn,
                """
                SELECT activation.*, active_task.run_id AS activation_run_id
                FROM agent_activations AS activation
                JOIN delegated_tasks AS active_task ON active_task.task_id = activation.task_id
                WHERE activation.session_id = ? AND activation.phase != 'released'
                ORDER BY activation.rowid DESC
                LIMIT 1
                """,
                (session.session_id,),
            )
            if current is not None and str(current["activation_run_id"]) != task.run_id:
                raise ConcurrentActivationError(
                    "persistent session is active in another orchestration run"
                )
            if current is not None and str(current["phase"]) == ActivationPhase.STOPPING.value:
                raise ConcurrentActivationError("persistent session activation is stopping")
            if planned_task_id is None:
                await self._insert_task(conn, task, now=now)
            else:
                board_task = await _fetchone(
                    conn,
                    "SELECT * FROM delegated_tasks WHERE task_id = ?",
                    (planned_task_id,),
                )
                if (
                    board_task is None
                    or str(board_task["task_id"]) != task.task_id
                    or str(board_task["run_id"]) != task.run_id
                    or not bool(board_task["board_only"])
                    or str(board_task["outcome"]) != TaskOutcome.PENDING.value
                    or str(board_task["board_status"])
                    not in {
                        TaskBoardStatus.PLANNED.value,
                        TaskBoardStatus.WORKING.value,
                    }
                    or str(board_task["owner_session_id"]) != parent_session_id
                ):
                    raise ValueError("planned task-board item is no longer assignable")
                await conn.execute(
                    """
                    UPDATE delegated_tasks
                    SET owner_session_id = ?, description = ?, acceptance_criteria = ?,
                        background = ?, board_only = 0, effective_tools_json = ?,
                        runtime_context_json = ?
                    WHERE task_id = ?
                    """,
                    (
                        session.session_id,
                        task.description,
                        task.acceptance_criteria,
                        int(task.background),
                        _json_dumps(sorted(task.effective_tools)),
                        _json_dumps(task.runtime_context),
                        task.task_id,
                    ),
                )
            await conn.execute(
                """
                UPDATE agent_sessions
                SET profile = ?, effective_tools_json = ?, lifecycle = 'active',
                    updated_at = ?, archived_at = NULL
                WHERE session_id = ?
                """,
                (
                    session.profile,
                    _json_dumps(sorted(session.effective_tools)),
                    now,
                    session.session_id,
                ),
            )
            if current is not None:
                await self._enqueue_message_in_transaction(
                    conn,
                    session_id=session.session_id,
                    kind="delegated_task",
                    payload={
                        "task_id": task.task_id,
                        "task_key": task.task_key,
                        "task": task.description,
                        "parent_task_id": task.parent_task_id,
                    },
                    now=now,
                    idempotency_key=f"delegated-task:{task.task_id}",
                )
                return _activation_from_row(current), True

            row = await _fetchone(
                conn,
                """
                SELECT COUNT(*) AS count
                FROM agent_session_attachments AS child
                JOIN agent_activations AS active ON active.session_id = child.session_id
                JOIN delegated_tasks AS active_task ON active_task.task_id = active.task_id
                WHERE child.run_id = ?
                  AND child.parent_session_id = ?
                  AND active_task.run_id = ?
                  AND active.phase IN ('starting', 'running', 'stopping')
                """,
                (task.run_id, parent_session_id, task.run_id),
            )
            assert row is not None
            activation.phase = (
                ActivationPhase.STARTING
                if int(row["count"]) < max_direct_children
                else ActivationPhase.QUEUED
            )
            await self._insert_activation(conn, activation)
        return activation, False

    async def get_activation(self, activation_id: str) -> AgentActivationRecord | None:
        async with self._read_transaction("get_activation") as conn:
            row = await _fetchone(
                conn,
                "SELECT * FROM agent_activations WHERE activation_id = ?",
                (activation_id,),
            )
            if row is not None:
                await conn.execute(
                    "UPDATE delegated_tasks SET board_status = 'working' WHERE task_id = ?",
                    (str(row["task_id"]),),
                )
        return None if row is None else _activation_from_row(row)

    async def latest_activation_for_task(
        self,
        task_id: str,
    ) -> AgentActivationRecord | None:
        async with self._read_transaction("latest_activation_for_task") as conn:
            row = await _fetchone(
                conn,
                """
                SELECT * FROM agent_activations
                WHERE task_id = ?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (task_id,),
            )
        return None if row is None else _activation_from_row(row)

    async def activation_count_for_task(self, task_id: str) -> int:
        async with self._read_transaction("activation_count_for_task") as conn:
            row = await _fetchone(
                conn,
                "SELECT COUNT(*) AS count FROM agent_activations WHERE task_id = ?",
                (task_id,),
            )
        assert row is not None
        return int(row["count"])

    async def activation_counts_by_task_key(
        self,
        run_id: str,
    ) -> tuple[tuple[str, int], ...]:
        """Return execution-attempt counts for every delegated task in a run."""

        async with self._read_transaction("activation_counts_by_task_key") as conn:
            rows = await _fetchall(
                conn,
                """
                SELECT task.task_key, COUNT(activation.activation_id) AS count
                FROM delegated_tasks AS task
                LEFT JOIN agent_activations AS activation
                    ON activation.task_id = task.task_id
                WHERE task.run_id = ?
                GROUP BY task.task_id, task.task_key
                """,
                (run_id,),
            )
        return tuple((str(row["task_key"]), int(row["count"])) for row in rows)

    async def current_activation_for_session(
        self,
        session_id: str,
    ) -> AgentActivationRecord | None:
        async with self._read_transaction("current_activation_for_session") as conn:
            row = await _fetchone(
                conn,
                """
                SELECT * FROM agent_activations
                WHERE session_id = ? AND phase != 'released'
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (session_id,),
            )
        return None if row is None else _activation_from_row(row)

    async def claim_activation(
        self,
        activation_id: str,
    ) -> AgentActivationRecord | None:
        """Atomically move one startable activation to running."""

        now = self._clock()
        async with self._transaction("claim_activation") as conn:
            cursor = await conn.execute(
                """
                UPDATE agent_activations
                SET phase = 'running', started_at = COALESCE(started_at, ?),
                    live_model_call = 1
                WHERE activation_id = ?
                  AND phase = 'starting'
                """,
                (now, activation_id),
            )
            try:
                if cursor.rowcount != 1:
                    return None
            finally:
                await cursor.close()
            row = await _fetchone(
                conn,
                "SELECT * FROM agent_activations WHERE activation_id = ?",
                (activation_id,),
            )
        return None if row is None else _activation_from_row(row)

    async def list_startable_activations(self) -> list[AgentActivationRecord]:
        async with self._read_transaction("list_startable_activations") as conn:
            rows = await _fetchall(
                conn,
                """
                SELECT * FROM agent_activations
                WHERE phase = 'starting'
                ORDER BY rowid ASC
                """,
            )
        return [_activation_from_row(row) for row in rows]

    async def list_orphaned_activations(self) -> list[AgentActivationRecord]:
        """Return activations left live by the previous Gateway process."""

        async with self._read_transaction("list_orphaned_activations") as conn:
            rows = await _fetchall(
                conn,
                """
                SELECT * FROM agent_activations
                WHERE phase IN ('running', 'stopping')
                ORDER BY rowid ASC
                """,
            )
        return [_activation_from_row(row) for row in rows]

    async def set_activation_phase(
        self,
        activation_id: str,
        phase: ActivationPhase,
    ) -> AgentActivationRecord:
        now = self._clock()
        async with self._transaction("set_activation_phase") as conn:
            cursor = await conn.execute(
                """
                UPDATE agent_activations
                SET phase = ?, started_at = CASE
                    WHEN ? = 'starting' AND started_at IS NULL THEN ?
                    ELSE started_at
                END
                WHERE activation_id = ?
                  AND phase != 'released'
                  AND (phase != 'stopping' OR ? = 'stopping')
                """,
                (phase.value, phase.value, now, activation_id, phase.value),
            )
            try:
                changed = cursor.rowcount
            finally:
                await cursor.close()
            if changed != 1:
                current = await _fetchone(
                    conn,
                    "SELECT phase FROM agent_activations WHERE activation_id = ?",
                    (activation_id,),
                )
                if current is None or str(current["phase"]) != "stopping":
                    raise ValueError(f"nonterminal activation not found: {activation_id}")
        activation = await self.get_activation(activation_id)
        if activation is None:
            raise ValueError(f"activation not found: {activation_id}")
        return activation

    async def set_activation_runtime_facts(
        self,
        activation_id: str,
        *,
        phase: ActivationPhase,
        live_model_call: bool = False,
        live_tool_call: bool = False,
        external_wait_id: str | None = None,
    ) -> AgentActivationRecord:
        async with self._transaction("set_activation_runtime_facts") as conn:
            cursor = await conn.execute(
                """
                UPDATE agent_activations
                SET phase = ?, live_model_call = ?, live_tool_call = ?,
                    external_wait_id = ?
                WHERE activation_id = ?
                  AND phase != 'released'
                  AND (phase != 'stopping' OR ? = 'stopping')
                """,
                (
                    phase.value,
                    int(live_model_call),
                    int(live_tool_call),
                    external_wait_id,
                    activation_id,
                    phase.value,
                ),
            )
            try:
                changed = cursor.rowcount
            finally:
                await cursor.close()
            if changed != 1:
                current = await _fetchone(
                    conn,
                    "SELECT phase FROM agent_activations WHERE activation_id = ?",
                    (activation_id,),
                )
                if current is None or str(current["phase"]) != "stopping":
                    raise ValueError(f"nonterminal activation not found: {activation_id}")
        activation = await self.get_activation(activation_id)
        if activation is None:
            raise ValueError(f"activation not found: {activation_id}")
        return activation

    async def refresh_parent_foreground_wait(
        self,
        *,
        run_id: str,
        parent_session_id: str,
        parent_task_id: str,
        parent_activation_id: str,
        preferred_child_activation_id: str | None = None,
    ) -> AgentActivationRecord | None:
        """Project all pending foreground children onto one parent activation.

        ``external_wait_id`` remains a compact public pointer, while the full
        wait set is derived transactionally from durable pending child tasks.
        This prevents one of several parallel foreground completions from
        making the parent appear to be working before the last child settles.
        """

        async with self._transaction("refresh_parent_foreground_wait") as conn:
            parent = await _fetchone(
                conn,
                """
                SELECT *
                FROM agent_activations
                WHERE activation_id = ?
                  AND session_id = ?
                  AND task_id = ?
                  AND phase NOT IN ('stopping', 'released')
                """,
                (parent_activation_id, parent_session_id, parent_task_id),
            )
            if parent is None:
                return None
            rows = await _fetchall(
                conn,
                """
                SELECT child_task.task_id, child_activation.activation_id
                FROM delegated_tasks AS child_task
                JOIN agent_session_attachments AS attachment
                  ON attachment.session_id = child_task.owner_session_id
                 AND attachment.run_id = child_task.run_id
                LEFT JOIN agent_activations AS child_activation
                  ON child_activation.task_id = child_task.task_id
                 AND child_activation.phase != 'released'
                WHERE child_task.run_id = ?
                  AND child_task.parent_task_id = ?
                  AND child_task.background = 0
                  AND child_task.outcome = 'pending'
                  AND attachment.parent_session_id = ?
                ORDER BY child_task.rowid
                """,
                (run_id, parent_task_id, parent_session_id),
            )
            waiting_ids = [str(row["activation_id"] or row["task_id"]) for row in rows]
            preferred = str(preferred_child_activation_id or "")
            external_wait_id = (
                preferred
                if preferred and preferred in waiting_ids
                else waiting_ids[0]
                if waiting_ids
                else None
            )
            await conn.execute(
                """
                UPDATE agent_activations
                SET phase = 'running', live_model_call = ?, live_tool_call = 0,
                    external_wait_id = ?
                WHERE activation_id = ?
                  AND session_id = ?
                  AND task_id = ?
                  AND phase NOT IN ('stopping', 'released')
                """,
                (
                    int(not waiting_ids),
                    external_wait_id,
                    parent_activation_id,
                    parent_session_id,
                    parent_task_id,
                ),
            )
            updated = await _fetchone(
                conn,
                "SELECT * FROM agent_activations WHERE activation_id = ?",
                (parent_activation_id,),
            )
        return None if updated is None else _activation_from_row(updated)

    async def list_live_subtree_activations(
        self,
        root_session_id: str,
        *,
        run_id: str | None = None,
    ) -> list[AgentActivationRecord]:
        run_filter = "" if run_id is None else " AND attachment.run_id = ?"
        async with self._read_transaction("list_live_subtree_activations") as conn:
            rows = await _fetchall(
                conn,
                """
                WITH RECURSIVE subtree(session_id, relative_depth) AS (
                    SELECT session_id, 0
                    FROM agent_session_attachments AS attachment
                    WHERE session_id = ?
                    """
                + run_filter
                + """
                    UNION ALL
                    SELECT child.session_id, parent.relative_depth + 1
                    FROM agent_session_attachments AS child
                    JOIN subtree AS parent
                      ON child.parent_session_id = parent.session_id
                    """
                + ("" if run_id is None else " AND child.run_id = ?")
                + """
                )
                SELECT activation.*
                FROM subtree
                JOIN agent_activations AS activation
                  ON activation.session_id = subtree.session_id
                JOIN delegated_tasks AS task ON task.task_id = activation.task_id
                WHERE activation.phase != 'released'
                """
                + ("" if run_id is None else " AND task.run_id = ?")
                + """
                ORDER BY subtree.relative_depth DESC, activation.rowid DESC
                """,
                (
                    (root_session_id,)
                    if run_id is None
                    else (root_session_id, run_id, run_id, run_id)
                ),
            )
        return [_activation_from_row(row) for row in rows]

    async def mark_subtree_stopping(
        self,
        root_session_id: str,
        *,
        run_id: str,
        expected_activation_id: str,
        reason: str,
    ) -> list[AgentActivationRecord]:
        """Atomically fence one activation generation and stop its live subtree."""

        async with self._transaction("mark_subtree_stopping") as conn:
            root = await _fetchone(
                conn,
                """
                SELECT activation.activation_id
                FROM agent_activations AS activation
                JOIN delegated_tasks AS task ON task.task_id = activation.task_id
                WHERE activation.session_id = ?
                  AND task.run_id = ?
                  AND activation.phase != 'released'
                ORDER BY activation.rowid DESC
                LIMIT 1
                """,
                (root_session_id, run_id),
            )
            if root is None or str(root["activation_id"]) != expected_activation_id:
                raise ConcurrentActivationError(
                    "activation generation does not match the current session activation"
                )
            rows = await _fetchall(
                conn,
                """
                WITH RECURSIVE subtree(session_id, relative_depth) AS (
                    SELECT session_id, 0
                    FROM agent_session_attachments
                    WHERE session_id = ? AND run_id = ?
                    UNION ALL
                    SELECT child.session_id, parent.relative_depth + 1
                    FROM agent_session_attachments AS child
                    JOIN subtree AS parent
                      ON child.parent_session_id = parent.session_id
                    WHERE child.run_id = ?
                )
                SELECT activation.*
                FROM subtree
                JOIN agent_activations AS activation
                  ON activation.session_id = subtree.session_id
                JOIN delegated_tasks AS task ON task.task_id = activation.task_id
                WHERE activation.phase != 'released' AND task.run_id = ?
                ORDER BY subtree.relative_depth DESC, activation.rowid DESC
                """,
                (root_session_id, run_id, run_id, run_id),
            )
            if not rows:
                raise ConcurrentActivationError("interrupt subtree has no live activation")
            activation_ids = [str(row["activation_id"]) for row in rows]
            placeholders = ", ".join("?" for _ in activation_ids)
            await conn.execute(
                f"""
                UPDATE agent_activations
                SET phase = 'stopping', terminal_reason = ?
                WHERE activation_id IN ({placeholders}) AND phase != 'released'
                """,
                (reason, *activation_ids),
            )
        return [
            replace(
                _activation_from_row(row),
                phase=ActivationPhase.STOPPING,
                terminal_reason=reason,
            )
            for row in rows
        ]

    async def live_direct_child_count(
        self,
        parent_session_id: str,
        *,
        run_id: str | None = None,
    ) -> int:
        async with self._read_transaction("live_direct_child_count") as conn:
            row = await _fetchone(
                conn,
                """
                SELECT COUNT(*) AS count
                FROM agent_session_attachments AS child
                JOIN agent_activations AS activation
                  ON activation.session_id = child.session_id
                JOIN delegated_tasks AS task ON task.task_id = activation.task_id
                WHERE child.parent_session_id = ?
                  AND child.run_id = task.run_id
                  AND activation.phase IN ('starting', 'running', 'stopping')
                """
                + ("" if run_id is None else " AND child.run_id = ?")
                + """
                """,
                (parent_session_id,) if run_id is None else (parent_session_id, run_id),
            )
        assert row is not None
        return int(row["count"])

    async def live_descendant_count(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
    ) -> int:
        async with self._read_transaction("live_descendant_count") as conn:
            row = await _fetchone(
                conn,
                """
                WITH RECURSIVE descendants(session_id) AS (
                    SELECT session_id
                    FROM agent_session_attachments
                    WHERE parent_session_id = ?
                    """
                + ("" if run_id is None else " AND run_id = ?")
                + """
                    UNION ALL
                    SELECT child.session_id
                    FROM agent_session_attachments AS child
                    JOIN descendants AS parent
                      ON child.parent_session_id = parent.session_id
                    """
                + ("" if run_id is None else " AND child.run_id = ?")
                + """
                )
                SELECT COUNT(*) AS count
                FROM descendants
                JOIN agent_activations AS activation
                  ON activation.session_id = descendants.session_id
                JOIN delegated_tasks AS task ON task.task_id = activation.task_id
                WHERE activation.phase != 'released'
                """
                + ("" if run_id is None else " AND task.run_id = ?")
                + """
                """,
                ((session_id,) if run_id is None else (session_id, run_id, run_id, run_id)),
            )
        assert row is not None
        return int(row["count"])

    async def oldest_queued_direct_child(
        self,
        parent_session_id: str,
        *,
        run_id: str | None = None,
    ) -> AgentActivationRecord | None:
        async with self._read_transaction("oldest_queued_direct_child") as conn:
            row = await _fetchone(
                conn,
                """
                SELECT activation.*
                FROM agent_session_attachments AS child
                JOIN agent_activations AS activation
                  ON activation.session_id = child.session_id
                JOIN delegated_tasks AS task ON task.task_id = activation.task_id
                WHERE child.parent_session_id = ?
                  AND child.run_id = task.run_id
                  AND activation.phase = 'queued'
                """
                + ("" if run_id is None else " AND child.run_id = ?")
                + """
                ORDER BY activation.rowid ASC
                LIMIT 1
                """,
                (parent_session_id,) if run_id is None else (parent_session_id, run_id),
            )
        return None if row is None else _activation_from_row(row)

    async def set_session_lifecycle(
        self,
        session_id: str,
        lifecycle: SessionLifecycle,
    ) -> None:
        now = self._clock()
        async with self._transaction("set_session_lifecycle") as conn:
            cursor = await conn.execute(
                """
                UPDATE agent_sessions
                SET lifecycle = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (lifecycle.value, now, session_id),
            )
            try:
                changed = cursor.rowcount
            finally:
                await cursor.close()
            if changed != 1:
                raise ValueError(f"agent session not found: {session_id}")

    async def release_activation(self, activation_id: str, *, reason: str) -> None:
        now = self._clock()
        async with self._transaction("release_activation") as conn:
            cursor = await conn.execute(
                """
                UPDATE agent_activations
                SET phase = 'released', live_model_call = 0, live_tool_call = 0,
                    external_wait_id = NULL, finished_at = ?, terminal_reason = ?
                WHERE activation_id = ? AND phase != 'released'
                """,
                (now, reason, activation_id),
            )
            try:
                changed = cursor.rowcount
            finally:
                await cursor.close()
            if changed != 1:
                raise ValueError(f"nonterminal activation not found: {activation_id}")

    async def finish_activation_bundle(
        self,
        activation_id: str,
        *,
        outcome: TaskOutcome,
        result: Mapping[str, Any],
        next_activation_id: str,
        continue_session: bool = True,
    ) -> tuple[AgentActivationRecord | None, tuple[str, ...]]:
        """Atomically settle work, release its instance, and schedule one successor."""

        if outcome is TaskOutcome.PENDING:
            raise ValueError("terminal task outcome is required")
        now = self._clock()
        async with self._transaction("finish_activation_bundle") as conn:
            row = await _fetchone(
                conn,
                """
                SELECT activation.*, task.run_id AS task_run_id,
                       task.task_key, task.background, task.outcome,
                       task.evidence_json,
                       attachment.parent_session_id AS attached_parent_session_id
                FROM agent_activations AS activation
                JOIN delegated_tasks AS task ON task.task_id = activation.task_id
                LEFT JOIN agent_session_attachments AS attachment
                  ON attachment.session_id = activation.session_id
                 AND attachment.run_id = task.run_id
                WHERE activation.activation_id = ?
                """,
                (activation_id,),
            )
            if row is None:
                raise ValueError(f"activation not found: {activation_id}")
            if str(row["phase"]) == ActivationPhase.RELEASED.value:
                raise ValueError(f"nonterminal activation not found: {activation_id}")
            if str(row["outcome"]) != TaskOutcome.PENDING.value:
                raise ValueError(f"pending task not found: {row['task_id']}")
            task_id = str(row["task_id"])
            session_id = str(row["session_id"])
            run_id = str(row["task_run_id"])
            parent_session_id = (
                None
                if row["attached_parent_session_id"] is None
                else str(row["attached_parent_session_id"])
            )
            evidence = list(_json_string_tuple(row["evidence_json"]))
            summary = str(result.get("summary") or result.get("error") or "").strip()
            if summary and summary not in evidence:
                evidence.append(summary)
            board_status = (
                TaskBoardStatus.COMPLETED
                if outcome is TaskOutcome.SUCCEEDED
                else TaskBoardStatus.BLOCKED
            )
            await conn.execute(
                """
                UPDATE delegated_tasks
                SET outcome = ?, result_json = ?, finished_at = ?,
                    board_status = ?, evidence_json = ?
                WHERE task_id = ? AND outcome = 'pending'
                """,
                (
                    outcome.value,
                    _json_dumps(result),
                    now,
                    board_status.value,
                    _json_dumps(evidence),
                    task_id,
                ),
            )
            await conn.execute(
                """
                UPDATE agent_inbox_messages
                SET acknowledged_at = ?
                WHERE kind = 'delegated_task'
                  AND acknowledged_at IS NULL
                  AND json_extract(payload_json, '$.task_id') = ?
                """,
                (now, task_id),
            )
            if bool(row["background"]) and parent_session_id is not None:
                await self._enqueue_message_in_transaction(
                    conn,
                    session_id=parent_session_id,
                    kind="child_result",
                    payload={
                        "task_id": task_id,
                        "activation_id": activation_id,
                        "task_key": str(row["task_key"]),
                        "session_id": session_id,
                        "outcome": outcome.value,
                        "result": dict(result),
                    },
                    now=now,
                    idempotency_key=f"child-result:{task_id}:{activation_id}",
                )
            await conn.execute(
                """
                UPDATE agent_activations
                SET phase = 'released', live_model_call = 0, live_tool_call = 0,
                    external_wait_id = NULL, finished_at = ?, terminal_reason = ?
                WHERE activation_id = ? AND phase != 'released'
                """,
                (now, outcome.value, activation_id),
            )

            pending = await _fetchall(
                conn,
                """
                SELECT * FROM delegated_tasks
                WHERE owner_session_id = ? AND run_id = ? AND outcome = 'pending'
                  AND board_only = 0
                ORDER BY rowid ASC
                """,
                (session_id, run_id),
            )
            interrupted_task_ids: list[str] = []
            if not continue_session:
                interrupted_result = _json_dumps(dict(result))
                for pending_row in pending:
                    pending_task_id = str(pending_row["task_id"])
                    interrupted_task_ids.append(pending_task_id)
                    await conn.execute(
                        """
                        UPDATE delegated_tasks
                        SET outcome = 'interrupted', result_json = ?, finished_at = ?,
                            retry_of_activation_id = COALESCE(
                                retry_of_activation_id, ?
                            )
                        WHERE task_id = ? AND outcome = 'pending'
                        """,
                        (interrupted_result, now, activation_id, pending_task_id),
                    )
                    await conn.execute(
                        """
                        UPDATE agent_inbox_messages
                        SET acknowledged_at = ?
                        WHERE kind = 'delegated_task'
                          AND acknowledged_at IS NULL
                          AND json_extract(payload_json, '$.task_id') = ?
                        """,
                        (now, pending_task_id),
                    )
                    if bool(pending_row["background"]) and parent_session_id is not None:
                        await self._enqueue_message_in_transaction(
                            conn,
                            session_id=parent_session_id,
                            kind="child_result",
                            payload={
                                "task_id": pending_task_id,
                                "activation_id": activation_id,
                                "task_key": str(pending_row["task_key"]),
                                "session_id": session_id,
                                "outcome": TaskOutcome.INTERRUPTED.value,
                                "result": dict(result),
                            },
                            now=now,
                            idempotency_key=(f"child-result:{pending_task_id}:{activation_id}"),
                        )
                pending = []

            if pending:
                next_task_id = str(pending[0]["task_id"])
                successor = AgentActivationRecord(
                    activation_id=next_activation_id,
                    session_id=session_id,
                    task_id=next_task_id,
                    phase=ActivationPhase.STARTING,
                )
                await self._insert_activation(conn, successor)
                await conn.execute(
                    """
                    UPDATE agent_sessions
                    SET lifecycle = 'active', updated_at = ?, archived_at = NULL
                    WHERE session_id = ?
                    """,
                    (now, session_id),
                )
                return successor, tuple(interrupted_task_ids)

            await conn.execute(
                """
                UPDATE agent_sessions
                SET lifecycle = 'idle', updated_at = ?
                WHERE session_id = ?
                """,
                (now, session_id),
            )
            if parent_session_id is None:
                return None, tuple(interrupted_task_ids)
            queued = await _fetchone(
                conn,
                """
                SELECT activation.*
                FROM agent_session_attachments AS child
                JOIN agent_activations AS activation ON activation.session_id = child.session_id
                JOIN delegated_tasks AS queued_task ON queued_task.task_id = activation.task_id
                WHERE child.run_id = ?
                  AND child.parent_session_id = ?
                  AND queued_task.run_id = ?
                  AND activation.phase = 'queued'
                ORDER BY activation.rowid ASC
                LIMIT 1
                """,
                (run_id, parent_session_id, run_id),
            )
            if queued is None:
                return None, tuple(interrupted_task_ids)
            await conn.execute(
                """
                UPDATE agent_activations
                SET phase = 'starting'
                WHERE activation_id = ? AND phase = 'queued'
                """,
                (str(queued["activation_id"]),),
            )
            promoted = await _fetchone(
                conn,
                "SELECT * FROM agent_activations WHERE activation_id = ?",
                (str(queued["activation_id"]),),
            )
            return (
                None if promoted is None else _activation_from_row(promoted),
                tuple(interrupted_task_ids),
            )

    async def mark_final_synthesis_completed(self, run_id: str) -> None:
        now = self._clock()
        result = _json_dumps({"summary": "root final synthesis completed"})
        async with self._transaction("mark_final_synthesis_completed") as conn:
            cursor = await conn.execute(
                """
                UPDATE orchestration_runs
                SET final_synthesis_completed = 1
                WHERE run_id = ? AND lifecycle = 'active'
                """,
                (run_id,),
            )
            try:
                changed = cursor.rowcount
            finally:
                await cursor.close()
            if changed != 1:
                raise ValueError(f"active orchestration run not found: {run_id}")
            await conn.execute(
                """
                UPDATE delegated_tasks
                SET outcome = 'succeeded', result_json = ?, finished_at = ?,
                    board_status = 'completed', evidence_json = ?
                WHERE task_id = (
                    SELECT root_task_id FROM orchestration_runs WHERE run_id = ?
                ) AND outcome = 'pending'
                """,
                (result, now, _json_dumps(["root final synthesis completed"]), run_id),
            )

    async def terminate_run_without_synthesis(
        self,
        run_id: str,
        *,
        root_outcome: TaskOutcome,
        result: Mapping[str, Any],
    ) -> bool:
        """Converge a failed or cancelled root after all executions are released."""

        if root_outcome not in {TaskOutcome.FAILED, TaskOutcome.INTERRUPTED}:
            raise ValueError("unsuccessful root outcome is required")
        now = self._clock()
        child_result = _json_dumps({"reason": "root_task_terminal"})
        async with self._transaction("terminate_run_without_synthesis") as conn:
            run = await _fetchone(
                conn,
                "SELECT * FROM orchestration_runs WHERE run_id = ?",
                (run_id,),
            )
            if run is None:
                raise ValueError(f"orchestration run not found: {run_id}")
            if str(run["lifecycle"]) != RunLifecycle.ACTIVE.value:
                return False
            live = await _fetchone(
                conn,
                """
                SELECT 1
                FROM agent_activations AS activation
                JOIN delegated_tasks AS task ON task.task_id = activation.task_id
                WHERE task.run_id = ? AND activation.phase != 'released'
                LIMIT 1
                """,
                (run_id,),
            )
            if live is not None:
                raise ConcurrentActivationError(
                    "cannot terminate orchestration run with live activations"
                )
            await conn.execute(
                """
                UPDATE delegated_tasks
                SET outcome = ?, result_json = ?, finished_at = ?
                WHERE task_id = ? AND outcome = 'pending'
                """,
                (
                    root_outcome.value,
                    _json_dumps(result),
                    now,
                    str(run["root_task_id"]),
                ),
            )
            await conn.execute(
                """
                UPDATE delegated_tasks
                SET outcome = 'interrupted', result_json = ?, finished_at = ?
                WHERE run_id = ? AND task_id != ? AND outcome = 'pending'
                """,
                (child_result, now, run_id, str(run["root_task_id"])),
            )
            await conn.execute(
                """
                UPDATE agent_inbox_messages
                SET acknowledged_at = COALESCE(acknowledged_at, ?),
                    processed_at = COALESCE(processed_at, ?),
                    delivery_owner = NULL,
                    delivery_lease_expires_at = NULL
                WHERE kind = 'child_result'
                  AND json_extract(payload_json, '$.task_id') IN (
                      SELECT task_id FROM delegated_tasks WHERE run_id = ?
                  )
                """,
                (now, now, run_id),
            )
            await conn.execute(
                """
                UPDATE agent_sessions
                SET lifecycle = 'idle', updated_at = ?
                WHERE lifecycle = 'active'
                  AND session_id IN (
                      SELECT session_id FROM agent_session_attachments WHERE run_id = ?
                  )
                """,
                (now, run_id),
            )
            cursor = await conn.execute(
                """
                UPDATE orchestration_runs
                SET lifecycle = 'completed', completed_at = ?
                WHERE run_id = ? AND lifecycle = 'active'
                """,
                (now, run_id),
            )
            try:
                return int(cursor.rowcount) == 1
            finally:
                await cursor.close()

    async def run_ready_for_final_synthesis(
        self,
        run_id: str,
        *,
        observing_task_id: str | None = None,
        observing_activation_id: str | None = None,
    ) -> bool:
        """Return whether every delegated result is settled and observed.

        A parent result-notification turn may count its own activation-scoped
        payload as observed while it is producing the synthesis. The lifecycle
        listener persists ``processed_at`` only after that exact turn succeeds.
        """

        observing_clause = ""
        params: list[Any] = [run_id]
        if observing_task_id is not None and observing_activation_id is not None:
            observing_clause = """
                            AND NOT (
                                json_extract(message.payload_json, '$.task_id') = ?
                                AND json_extract(
                                    message.payload_json, '$.activation_id'
                                ) = ?
                            )
            """
            params.extend([observing_task_id, observing_activation_id])

        async with self._read_transaction("run_ready_for_final_synthesis") as conn:
            row = await _fetchone(
                conn,
                """
                SELECT 1 AS ready
                FROM orchestration_runs AS run
                WHERE run.run_id = ?
                  AND run.lifecycle = 'active'
                  AND NOT EXISTS (
                      SELECT 1 FROM delegated_tasks AS task
                      WHERE task.run_id = run.run_id
                        AND task.task_id != run.root_task_id
                        AND task.outcome = 'pending'
                        AND task.board_only = 0
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM agent_activations AS activation
                      JOIN delegated_tasks AS active_task
                        ON active_task.task_id = activation.task_id
                      WHERE active_task.run_id = run.run_id
                        AND activation.phase != 'released'
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM agent_inbox_messages AS message
                      JOIN delegated_tasks AS result_task
                        ON result_task.task_id = json_extract(message.payload_json, '$.task_id')
                      WHERE result_task.run_id = run.run_id
                        AND message.kind = 'child_result'
                        AND (
                            message.acknowledged_at IS NULL
                            OR message.processed_at IS NULL
                        )
                """
                + observing_clause
                + """
                  )
                LIMIT 1
                """,
                params,
            )
        return row is not None

    async def complete_run_if_eligible(self, run_id: str) -> bool:
        now = self._clock()
        async with self._transaction("complete_run_if_eligible") as conn:
            cursor = await conn.execute(
                """
                UPDATE orchestration_runs
                SET lifecycle = 'completed', completed_at = ?
                WHERE run_id = ?
                  AND lifecycle = 'active'
                  AND final_synthesis_completed = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM delegated_tasks
                      WHERE delegated_tasks.run_id = orchestration_runs.run_id
                        AND outcome = 'pending'
                        AND board_only = 0
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM agent_activations
                      JOIN delegated_tasks AS active_task
                        ON active_task.task_id = agent_activations.task_id
                      WHERE active_task.run_id = orchestration_runs.run_id
                        AND agent_activations.phase != 'released'
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM agent_inbox_messages
                      JOIN delegated_tasks AS message_task
                        ON message_task.task_id = json_extract(
                            agent_inbox_messages.payload_json,
                            '$.task_id'
                        )
                      WHERE message_task.run_id = orchestration_runs.run_id
                        AND (
                            agent_inbox_messages.acknowledged_at IS NULL
                            OR (
                                agent_inbox_messages.kind = 'child_result'
                                AND agent_inbox_messages.processed_at IS NULL
                            )
                        )
                  )
                """,
                (now, run_id),
            )
            try:
                return int(cursor.rowcount) == 1
            finally:
                await cursor.close()

    async def archive_eligible_runs(self, *, cutoff: int) -> list[str]:
        """Logically archive stable completed runs; no records are deleted."""

        now = self._clock()
        async with self._transaction("archive_eligible_runs") as conn:
            rows = await _fetchall(
                conn,
                """
                SELECT run_id FROM orchestration_runs
                WHERE lifecycle = 'completed' AND completed_at <= ?
                ORDER BY completed_at ASC, run_id ASC
                """,
                (cutoff,),
            )
            run_ids = [str(row["run_id"]) for row in rows]
            for run_id in run_ids:
                await conn.execute(
                    """
                    UPDATE orchestration_runs
                    SET lifecycle = 'archived', archived_at = ?
                    WHERE run_id = ? AND lifecycle = 'completed'
                    """,
                    (now, run_id),
                )
                await conn.execute(
                    """
                    UPDATE agent_sessions
                    SET lifecycle = 'archived', archived_at = ?, updated_at = ?
                    WHERE session_id IN (
                        SELECT session_id FROM agent_session_attachments WHERE run_id = ?
                    )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM agent_session_attachments AS live_attachment
                          JOIN orchestration_runs AS live_run
                            ON live_run.run_id = live_attachment.run_id
                          WHERE live_attachment.session_id = agent_sessions.session_id
                            AND live_run.lifecycle != 'archived'
                      )
                    """,
                    (now, now, run_id),
                )
        return run_ids

    async def enqueue_message(
        self,
        session_id: str,
        *,
        kind: str,
        payload: Mapping[str, Any],
    ) -> InboxMessageRecord:
        now = self._clock()
        async with self._transaction("enqueue_message") as conn:
            return await self._enqueue_message_in_transaction(
                conn,
                session_id=session_id,
                kind=kind,
                payload=payload,
                now=now,
            )

    async def acknowledge_message(self, message_id: str) -> None:
        now = self._clock()
        async with self._transaction("acknowledge_message") as conn:
            cursor = await conn.execute(
                """
                UPDATE agent_inbox_messages
                SET acknowledged_at = ?
                WHERE message_id = ? AND acknowledged_at IS NULL
                """,
                (now, message_id),
            )
            try:
                changed = cursor.rowcount
            finally:
                await cursor.close()
            if changed != 1:
                raise ValueError(f"pending inbox message not found: {message_id}")

    async def acknowledge_delegated_task_message(self, task_id: str) -> None:
        """Mark the durable append command consumed after its task settles."""

        now = self._clock()
        async with self._transaction("acknowledge_delegated_task_message") as conn:
            await conn.execute(
                """
                UPDATE agent_inbox_messages
                SET acknowledged_at = ?
                WHERE kind = 'delegated_task'
                  AND acknowledged_at IS NULL
                  AND json_extract(payload_json, '$.task_id') = ?
                """,
                (now, task_id),
            )

    async def acknowledge_child_result_message(
        self,
        task_id: str,
        *,
        activation_id: str | None = None,
        owner: str | None = None,
    ) -> None:
        """Mark a child result delivered only after its parent accepted it."""

        now = self._clock()
        async with self._transaction("acknowledge_child_result_message") as conn:
            activation_clause = (
                ""
                if activation_id is None
                else " AND json_extract(payload_json, '$.activation_id') = ?"
            )
            owner_clause = "" if owner is None else " AND delivery_owner = ?"
            params: tuple[Any, ...] = (now, task_id)
            if activation_id is not None:
                params += (activation_id,)
            if owner is not None:
                params += (owner,)
            await conn.execute(
                """
                UPDATE agent_inbox_messages
                SET acknowledged_at = ?, delivery_owner = NULL,
                    delivery_lease_expires_at = NULL
                WHERE kind = 'child_result'
                  AND acknowledged_at IS NULL
                  AND json_extract(payload_json, '$.task_id') = ?
                """
                + activation_clause
                + owner_clause,
                params,
            )

    async def mark_child_result_processed(
        self,
        task_id: str,
        *,
        activation_id: str | None = None,
    ) -> None:
        now = self._clock()
        async with self._transaction("mark_child_result_processed") as conn:
            activation_clause = (
                ""
                if activation_id is None
                else " AND json_extract(payload_json, '$.activation_id') = ?"
            )
            params: tuple[Any, ...] = (now, task_id)
            if activation_id is not None:
                params += (activation_id,)
            cursor = await conn.execute(
                """
                UPDATE agent_inbox_messages
                SET processed_at = COALESCE(processed_at, ?)
                WHERE kind = 'child_result'
                  AND json_extract(payload_json, '$.task_id') = ?
                """
                + activation_clause,
                params,
            )
            try:
                if cursor.rowcount != 1:
                    raise ValueError(f"child result not found: {task_id}")
            finally:
                await cursor.close()

    async def observe_child_result_message(
        self,
        task_id: str,
        *,
        activation_id: str,
    ) -> None:
        """Atomically acknowledge and process one activation-scoped result."""

        now = self._clock()
        async with self._transaction("observe_child_result_message") as conn:
            cursor = await conn.execute(
                """
                UPDATE agent_inbox_messages
                SET acknowledged_at = COALESCE(acknowledged_at, ?),
                    processed_at = COALESCE(processed_at, ?),
                    delivery_owner = NULL, delivery_lease_expires_at = NULL
                WHERE kind = 'child_result'
                  AND json_extract(payload_json, '$.task_id') = ?
                  AND json_extract(payload_json, '$.activation_id') = ?
                """,
                (now, now, task_id, activation_id),
            )
            try:
                if cursor.rowcount != 1:
                    raise ValueError(
                        f"activation-scoped child result not found: {task_id}:{activation_id}"
                    )
            finally:
                await cursor.close()

    async def child_result_needs_delivery(
        self,
        task_id: str,
        *,
        activation_id: str,
    ) -> bool:
        """Return false after foreground observation or superseding retry."""

        async with self._read_transaction("child_result_needs_delivery") as conn:
            row = await _fetchone(
                conn,
                """
                SELECT 1 AS needed
                FROM agent_inbox_messages
                WHERE kind = 'child_result'
                  AND acknowledged_at IS NULL
                  AND processed_at IS NULL
                  AND json_extract(payload_json, '$.task_id') = ?
                  AND json_extract(payload_json, '$.activation_id') = ?
                LIMIT 1
                """,
                (task_id, activation_id),
            )
        return row is not None

    async def requeue_child_result_delivery(
        self,
        task_id: str,
        *,
        activation_id: str | None = None,
    ) -> bool:
        """Make an accepted-but-failed parent notification deliverable again."""

        async with self._transaction("requeue_child_result_delivery") as conn:
            activation_clause = (
                ""
                if activation_id is None
                else " AND json_extract(payload_json, '$.activation_id') = ?"
            )
            params: tuple[Any, ...] = (task_id,)
            if activation_id is not None:
                params += (activation_id,)
            cursor = await conn.execute(
                """
                UPDATE agent_inbox_messages
                SET acknowledged_at = NULL, delivery_owner = NULL,
                    delivery_lease_expires_at = NULL
                WHERE kind = 'child_result'
                  AND processed_at IS NULL
                  AND json_extract(payload_json, '$.task_id') = ?
                """
                + activation_clause,
                params,
            )
            try:
                return int(cursor.rowcount) == 1
            finally:
                await cursor.close()

    async def release_child_result_claim(
        self,
        task_id: str,
        *,
        activation_id: str | None = None,
        owner: str,
    ) -> None:
        async with self._transaction("release_child_result_claim") as conn:
            activation_clause = (
                ""
                if activation_id is None
                else " AND json_extract(payload_json, '$.activation_id') = ?"
            )
            params: tuple[Any, ...] = (task_id,)
            if activation_id is not None:
                params += (activation_id,)
            params += (owner,)
            await conn.execute(
                """
                UPDATE agent_inbox_messages
                SET delivery_owner = NULL, delivery_lease_expires_at = NULL
                WHERE kind = 'child_result'
                  AND acknowledged_at IS NULL
                  AND json_extract(payload_json, '$.task_id') = ?
                """
                + activation_clause
                + """
                  AND delivery_owner = ?
                """,
                params,
            )

    async def list_pending_messages(self, session_id: str) -> list[InboxMessageRecord]:
        async with self._read_transaction("list_pending_messages") as conn:
            rows = await _fetchall(
                conn,
                """
                SELECT * FROM agent_inbox_messages
                WHERE session_id = ? AND acknowledged_at IS NULL
                ORDER BY sequence ASC
                """,
                (session_id,),
            )
        return [_inbox_from_row(row) for row in rows]

    async def list_pending_child_result_messages(
        self,
        *,
        limit: int = 100,
    ) -> list[InboxMessageRecord]:
        if limit < 1:
            raise ValueError("limit must be positive")
        async with self._read_transaction("list_pending_child_result_messages") as conn:
            rows = await _fetchall(
                conn,
                """
                SELECT * FROM agent_inbox_messages
                WHERE kind = 'child_result' AND acknowledged_at IS NULL
                ORDER BY created_at ASC, message_id ASC
                LIMIT ?
                """,
                (limit,),
            )
        return [_inbox_from_row(row) for row in rows]

    async def list_unprocessed_child_result_messages(
        self,
        *,
        limit: int = 100,
    ) -> list[InboxMessageRecord]:
        if limit < 1:
            raise ValueError("limit must be positive")
        async with self._read_transaction("list_unprocessed_child_result_messages") as conn:
            rows = await _fetchall(
                conn,
                """
                SELECT * FROM agent_inbox_messages
                WHERE kind = 'child_result' AND processed_at IS NULL
                ORDER BY created_at ASC, message_id ASC
                LIMIT ?
                """,
                (limit,),
            )
        return [_inbox_from_row(row) for row in rows]

    async def claim_child_result_messages(
        self,
        *,
        owner: str,
        lease_expires_at: int,
        limit: int = 100,
        task_id: str | None = None,
        activation_id: str | None = None,
    ) -> list[InboxMessageRecord]:
        """Lease undelivered result messages so only one dispatcher can send them."""

        if not owner.strip():
            raise ValueError("delivery owner must not be empty")
        if limit < 1:
            raise ValueError("limit must be positive")
        now = self._clock()
        task_filter = ""
        params: list[Any] = [now]
        if task_id is not None:
            task_filter = " AND json_extract(payload_json, '$.task_id') = ?"
            params.append(task_id)
        if activation_id is not None:
            task_filter += " AND json_extract(payload_json, '$.activation_id') = ?"
            params.append(activation_id)
        params.append(limit)
        async with self._transaction("claim_child_result_messages") as conn:
            rows = await _fetchall(
                conn,
                """
                SELECT message_id
                FROM agent_inbox_messages
                WHERE kind = 'child_result'
                  AND acknowledged_at IS NULL
                  AND (delivery_owner IS NULL OR delivery_lease_expires_at <= ?)
                """
                + task_filter
                + " ORDER BY created_at ASC, message_id ASC LIMIT ?",
                params,
            )
            message_ids = [str(row["message_id"]) for row in rows]
            if not message_ids:
                return []
            placeholders = ",".join("?" for _ in message_ids)
            await conn.execute(
                f"""
                UPDATE agent_inbox_messages
                SET delivery_owner = ?, delivery_lease_expires_at = ?,
                    delivery_attempts = delivery_attempts + 1
                WHERE message_id IN ({placeholders})
                  AND acknowledged_at IS NULL
                  AND (delivery_owner IS NULL OR delivery_lease_expires_at <= ?)
                """,
                (owner, lease_expires_at, *message_ids, now),
            )
            claimed = await _fetchall(
                conn,
                f"""
                SELECT * FROM agent_inbox_messages
                WHERE message_id IN ({placeholders}) AND delivery_owner = ?
                ORDER BY created_at ASC, message_id ASC
                """,
                (*message_ids, owner),
            )
        return [_inbox_from_row(row) for row in claimed]


__all__ = [
    "ConcurrentActivationError",
    "OrchestrationRepository",
    "SessionStorageBinding",
]
