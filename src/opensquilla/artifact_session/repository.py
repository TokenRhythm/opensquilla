"""Transactional SQLite repository for durable ArtifactSession state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
import weakref
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, Protocol, cast

from opensquilla.compat import aiosqlite

from .errors import (
    ArtifactConflictError,
    ArtifactNotFoundError,
    ArtifactValidationError,
    WriterLeaseConflictError,
    WriterLeaseExpiredError,
)
from .models import (
    Actor,
    ActorKind,
    Anchor,
    AnchorKind,
    AnchorState,
    ArtifactBlobRef,
    ArtifactKind,
    AuditEvent,
    ChangeSet,
    ChangeSetStatus,
    CommitResult,
    Document,
    DocumentImportAttempt,
    DocumentImportMode,
    DocumentImportResult,
    DocumentPublication,
    DocumentPublishAttempt,
    DocumentPublishResult,
    DocumentSourceBinding,
    DocumentSourceType,
    EditSession,
    EditSessionMode,
    EditSessionStatus,
    MutationAttempt,
    MutationAttemptStatus,
    PreparedPromptAnnotationTarget,
    PromptAnnotation,
    PromptAnnotationStatus,
    Revision,
    RevisionSource,
    WriterLease,
)
from .schema import SCHEMA_STATEMENTS

TransactionFactory = Callable[[str], AbstractAsyncContextManager[Any]]
Clock = Callable[[], int]
IdFactory = Callable[[str], str]

MAX_PROMPT_ANNOTATIONS_PER_BATCH = 16
MAX_PROMPT_ANNOTATION_BODY_BYTES = 16_384
_MUTATION_ATTEMPT_TURN_QUERY_CHUNK_SIZE = 400


class _SessionStorageBinding(Protocol):
    """Narrow transaction seam exposed by SessionStorage."""

    def _write_transaction(
        self,
        operation: str,
        *,
        budget_seconds: float | None = None,
    ) -> AbstractAsyncContextManager[Any]: ...

    def read_transaction(
        self,
        operation: str,
    ) -> AbstractAsyncContextManager[Any]: ...

    @property
    def connection_generation(self) -> int: ...


class _StorageInitializationState:
    """Per-SessionStorage schema state without retaining the storage itself."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.generation = -1


_STORAGE_INITIALIZATION: weakref.WeakKeyDictionary[
    _SessionStorageBinding, _StorageInitializationState
] = weakref.WeakKeyDictionary()


def _storage_initialization_state(
    storage: _SessionStorageBinding,
) -> _StorageInitializationState:
    state = _STORAGE_INITIALIZATION.get(storage)
    if state is None:
        state = _StorageInitializationState()
        _STORAGE_INITIALIZATION[storage] = state
    return state


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(raw: Any) -> dict[str, Any]:
    parsed = json.loads(str(raw))
    if not isinstance(parsed, dict):
        raise ArtifactValidationError("stored JSON value is not an object")
    return cast(dict[str, Any], parsed)


def _json_object_or_none(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    return _json_object(raw)


def _json_operations(raw: Any) -> tuple[dict[str, Any], ...]:
    parsed = json.loads(str(raw))
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise ArtifactValidationError("stored change-set operations are invalid")
    return tuple(cast(dict[str, Any], item) for item in parsed)


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


def _document_from_row(row: Any) -> Document:
    data = dict(row)
    data["kind"] = ArtifactKind(data["kind"])
    return Document(**data)


def _revision_from_row(row: Any) -> Revision:
    data = dict(row)
    data["source"] = RevisionSource(data["source"])
    data["actor_kind"] = ActorKind(data["actor_kind"])
    return Revision(**data)


def _change_set_from_row(row: Any) -> ChangeSet:
    data = dict(row)
    data["status"] = ChangeSetStatus(data["status"])
    data["operations"] = _json_operations(data.pop("operations_json"))
    data["validation"] = _json_object_or_none(data.pop("validation_json"))
    data["created_by_kind"] = ActorKind(data["created_by_kind"])
    return ChangeSet(**data)


def _anchor_from_row(row: Any) -> Anchor:
    data = dict(row)
    data["kind"] = AnchorKind(data["kind"])
    data["state"] = AnchorState(data["state"])
    data["locator"] = _json_object(data.pop("locator_json"))
    data["context"] = _json_object_or_none(data.pop("context_json"))
    return Anchor(**data)


def _prompt_annotation_from_row(row: Any) -> PromptAnnotation:
    data = dict(row)
    data["status"] = PromptAnnotationStatus(data["status"])
    return PromptAnnotation(**data)


def _mutation_attempt_from_row(row: Any) -> MutationAttempt:
    data = dict(row)
    data["status"] = MutationAttemptStatus(data["status"])
    return MutationAttempt(**data)


def _document_source_binding_from_row(row: Any) -> DocumentSourceBinding:
    data = dict(row)
    data["source_type"] = DocumentSourceType(data["source_type"])
    data["mode"] = DocumentImportMode(data["mode"])
    return DocumentSourceBinding(**data)


def _document_import_attempt_from_row(row: Any) -> DocumentImportAttempt:
    data = dict(row)
    data["source_type"] = DocumentSourceType(data["source_type"])
    data["mode"] = DocumentImportMode(data["mode"])
    data["status"] = MutationAttemptStatus(data["status"])
    return DocumentImportAttempt(**data)


def _document_publication_from_row(row: Any) -> DocumentPublication:
    data = dict(row)
    data["created_by_kind"] = ActorKind(data["created_by_kind"])
    return DocumentPublication(**data)


def _document_publish_attempt_from_row(row: Any) -> DocumentPublishAttempt:
    data = dict(row)
    data["status"] = MutationAttemptStatus(data["status"])
    return DocumentPublishAttempt(**data)


async def get_prompt_annotation_on_conn(conn: Any, annotation_id: str) -> PromptAnnotation:
    """Load one annotation using a caller-owned transaction connection."""

    row = await _fetchone(
        conn,
        "SELECT * FROM artifact_prompt_annotations WHERE annotation_id = ?",
        (annotation_id,),
    )
    if row is None:
        raise ArtifactNotFoundError(f"prompt annotation not found: {annotation_id}")
    return _prompt_annotation_from_row(row)


async def preflight_prompt_annotations_on_conn(
    conn: Any,
    *,
    annotation_ids: Sequence[str],
    session_key: str,
    session_id: str,
    session_epoch: int,
    require_current_head: bool = True,
) -> tuple[PromptAnnotation, ...]:
    """Validate an ordered annotation batch without opening or committing a transaction."""

    ids = tuple(annotation_ids)
    if len(ids) > MAX_PROMPT_ANNOTATIONS_PER_BATCH:
        raise ArtifactValidationError("a prompt annotation batch may contain at most 16 items")
    if len(set(ids)) != len(ids):
        raise ArtifactValidationError("prompt annotation ids must be unique")
    if not ids:
        return ()

    annotations: list[PromptAnnotation] = []
    for annotation_id in ids:
        annotation = await get_prompt_annotation_on_conn(conn, annotation_id)
        if (
            annotation.session_key != session_key
            or annotation.session_id != session_id
            or annotation.session_epoch != session_epoch
        ):
            raise ArtifactNotFoundError(f"prompt annotation not found: {annotation_id}")
        if annotation.status is not PromptAnnotationStatus.DRAFT:
            raise ArtifactConflictError("prompt annotation is no longer a draft")
        if not annotation.body.strip():
            raise ArtifactValidationError("prompt annotation body must not be empty when sent")
        if len(annotation.body.encode("utf-8")) > MAX_PROMPT_ANNOTATION_BODY_BYTES:
            raise ArtifactValidationError("prompt annotation body exceeds 16 KiB")
        annotations.append(annotation)

    document_ids = {annotation.document_id for annotation in annotations}
    revision_ids = {annotation.revision_id for annotation in annotations}
    if len(document_ids) != 1 or (require_current_head and len(revision_ids) != 1):
        raise ArtifactValidationError(
            "a prompt annotation batch must target one document"
            + (" revision" if require_current_head else "")
        )
    document_id = annotations[0].document_id
    row = await _fetchone(
        conn,
        """
        SELECT session_key, session_id, head_revision_id
        FROM artifact_documents
        WHERE document_id = ?
        """,
        (document_id,),
    )
    if (
        row is None
        or str(row["session_key"]) != session_key
        or str(row["session_id"]) != session_id
    ):
        raise ArtifactNotFoundError(f"document not found: {document_id}")
    if require_current_head and str(row["head_revision_id"]) != annotations[0].revision_id:
        raise ArtifactConflictError("prompt annotation revision is no longer current")

    for annotation in annotations:
        anchor_row = await _fetchone(
            conn,
            """
            SELECT document_id, revision_id, state
            FROM artifact_anchors
            WHERE anchor_id = ?
            """,
            (annotation.anchor_id,),
        )
        if (
            anchor_row is None
            or str(anchor_row["document_id"]) != document_id
            or str(anchor_row["revision_id"]) != annotation.revision_id
            or (
                require_current_head
                and str(anchor_row["state"]) != AnchorState.RESOLVED.value
            )
        ):
            raise ArtifactConflictError("prompt annotation anchor is no longer valid")
    return tuple(annotations)


async def consume_prepared_prompt_annotations_on_conn(
    conn: Any,
    *,
    prepared_targets: Sequence[PreparedPromptAnnotationTarget],
    session_key: str,
    session_id: str,
    session_epoch: int,
    message_id: str,
    turn_id: str,
    updated_at: int,
) -> tuple[PromptAnnotation, ...]:
    """Atomically rebind a normalized batch and mark it sent.

    Source parsing is intentionally completed before this transaction.  The
    immutable draft snapshots, previous anchors, and current document head are
    fenced again here so a concurrent save rolls the entire turn acceptance
    back before the task can run.
    """

    prepared = tuple(prepared_targets)
    if not prepared:
        return ()
    if len(prepared) > MAX_PROMPT_ANNOTATIONS_PER_BATCH:
        raise ArtifactValidationError("a prompt annotation batch may contain at most 16 items")
    if len({item.expected_annotation.annotation_id for item in prepared}) != len(prepared):
        raise ArtifactValidationError("prompt annotation ids must be unique")
    if len({item.anchor_id for item in prepared}) != len(prepared):
        raise ArtifactValidationError("prepared prompt annotation anchor ids must be unique")
    if not message_id.strip() or not turn_id.strip():
        raise ArtifactValidationError("message_id and turn_id must not be empty")
    if isinstance(updated_at, bool) or updated_at < 0:
        raise ArtifactValidationError("updated_at must be a non-negative integer")

    expected = tuple(item.expected_annotation for item in prepared)
    current = await preflight_prompt_annotations_on_conn(
        conn,
        annotation_ids=tuple(item.annotation_id for item in expected),
        session_key=session_key,
        session_id=session_id,
        session_epoch=session_epoch,
        require_current_head=False,
    )
    document_ids = {item.document_id for item in expected}
    revision_ids = {item.revision_id for item in prepared}
    if len(document_ids) != 1 or len(revision_ids) != 1:
        raise ArtifactValidationError("prepared prompt annotations must target one current head")
    document_id = next(iter(document_ids))
    current_revision_id = next(iter(revision_ids))
    document_row = await _fetchone(
        conn,
        """
        SELECT session_key, session_id, head_revision_id
        FROM artifact_documents
        WHERE document_id = ?
        """,
        (document_id,),
    )
    if (
        document_row is None
        or str(document_row["session_key"]) != session_key
        or str(document_row["session_id"]) != session_id
    ):
        raise ArtifactNotFoundError(f"document not found: {document_id}")
    if str(document_row["head_revision_id"]) != current_revision_id:
        raise ArtifactConflictError("document changed while prompt annotations were prepared")
    revision_row = await _fetchone(
        conn,
        "SELECT document_id FROM artifact_revisions WHERE revision_id = ?",
        (current_revision_id,),
    )
    if revision_row is None or str(revision_row["document_id"]) != document_id:
        raise ArtifactConflictError("prepared prompt annotation revision is unavailable")

    for prepared_item, expected_item, current_item in zip(
        prepared,
        expected,
        current,
        strict=True,
    ):
        expected_hash = hashlib.sha256(expected_item.body.encode("utf-8")).digest()
        current_hash = hashlib.sha256(current_item.body.encode("utf-8")).digest()
        if (
            expected_item.annotation_id != current_item.annotation_id
            or expected_item.state_revision != current_item.state_revision
            or expected_item.document_id != current_item.document_id
            or expected_item.revision_id != current_item.revision_id
            or expected_item.anchor_id != current_item.anchor_id
            or prepared_item.previous_anchor_id != current_item.anchor_id
            or expected_hash != current_hash
        ):
            raise ArtifactConflictError("prompt annotation changed after normalization")
        previous_anchor = await _fetchone(
            conn,
            """
            SELECT document_id, revision_id
            FROM artifact_anchors
            WHERE anchor_id = ?
            """,
            (prepared_item.previous_anchor_id,),
        )
        if (
            previous_anchor is None
            or str(previous_anchor["document_id"]) != document_id
            or str(previous_anchor["revision_id"]) != current_item.revision_id
        ):
            raise ArtifactConflictError("prompt annotation source anchor changed")
        await conn.execute(
            """
            INSERT INTO artifact_anchors (
                anchor_id, document_id, revision_id, kind, locator_json,
                quote, context_json, state, remapped_from_anchor_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prepared_item.anchor_id,
                document_id,
                current_revision_id,
                prepared_item.kind.value,
                _json_dumps(prepared_item.locator),
                prepared_item.quote,
                _json_dumps(prepared_item.context),
                prepared_item.state.value,
                prepared_item.previous_anchor_id,
                updated_at,
            ),
        )
        await conn.execute(
            """
            INSERT INTO artifact_audit_events (
                event_id, document_id, event_type, actor_kind, actor_id,
                revision_id, anchor_id, payload_json, created_at
            ) VALUES (?, ?, 'anchor.remapped', ?, ?, ?, ?, ?, ?)
            """,
            (
                prepared_item.audit_event_id,
                document_id,
                prepared_item.actor_kind.value,
                prepared_item.actor_id,
                current_revision_id,
                prepared_item.anchor_id,
                _json_dumps(
                    {
                        "kind": prepared_item.kind.value,
                        "remapped_from_anchor_id": prepared_item.previous_anchor_id,
                        "state": prepared_item.state.value,
                    }
                ),
                updated_at,
            ),
        )

    consumed: list[PromptAnnotation] = []
    for sent_order, (prepared_item, annotation) in enumerate(zip(prepared, current, strict=True)):
        cursor = await conn.execute(
            """
            UPDATE artifact_prompt_annotations
            SET revision_id = ?, anchor_id = ?, status = ?,
                state_revision = state_revision + 1,
                sent_message_id = ?, sent_turn_id = ?, sent_order = ?, updated_at = ?
            WHERE annotation_id = ? AND status = ? AND state_revision = ?
              AND revision_id = ? AND anchor_id = ? AND body = ?
            """,
            (
                current_revision_id,
                prepared_item.anchor_id,
                PromptAnnotationStatus.SENT.value,
                message_id,
                turn_id,
                sent_order,
                updated_at,
                annotation.annotation_id,
                PromptAnnotationStatus.DRAFT.value,
                annotation.state_revision,
                annotation.revision_id,
                annotation.anchor_id,
                annotation.body,
            ),
        )
        try:
            if cursor.rowcount != 1:
                raise ArtifactConflictError("prompt annotation compare-and-swap failed")
        finally:
            await cursor.close()
        consumed.append(await get_prompt_annotation_on_conn(conn, annotation.annotation_id))
    return tuple(consumed)


async def consume_prompt_annotations_on_conn(
    conn: Any,
    *,
    expected_annotations: Sequence[PromptAnnotation],
    session_key: str,
    session_id: str,
    session_epoch: int,
    message_id: str,
    turn_id: str,
    updated_at: int,
) -> tuple[PromptAnnotation, ...]:
    """Atomically fence and mark a preflighted batch sent on a caller-owned connection."""

    expected = tuple(expected_annotations)
    current = await preflight_prompt_annotations_on_conn(
        conn,
        annotation_ids=tuple(item.annotation_id for item in expected),
        session_key=session_key,
        session_id=session_id,
        session_epoch=session_epoch,
    )
    if not message_id.strip() or not turn_id.strip():
        raise ArtifactValidationError("message_id and turn_id must not be empty")
    if isinstance(updated_at, bool) or updated_at < 0:
        raise ArtifactValidationError("updated_at must be a non-negative integer")

    for expected_item, current_item in zip(expected, current, strict=True):
        expected_hash = hashlib.sha256(expected_item.body.encode("utf-8")).digest()
        current_hash = hashlib.sha256(current_item.body.encode("utf-8")).digest()
        if (
            expected_item.annotation_id != current_item.annotation_id
            or expected_item.state_revision != current_item.state_revision
            or expected_item.document_id != current_item.document_id
            or expected_item.revision_id != current_item.revision_id
            or expected_item.anchor_id != current_item.anchor_id
            or expected_hash != current_hash
        ):
            raise ArtifactConflictError("prompt annotation changed after preflight")

    consumed: list[PromptAnnotation] = []
    for sent_order, annotation in enumerate(current):
        cursor = await conn.execute(
            """
            UPDATE artifact_prompt_annotations
            SET status = ?, state_revision = state_revision + 1,
                sent_message_id = ?, sent_turn_id = ?, sent_order = ?, updated_at = ?
            WHERE annotation_id = ? AND status = ? AND state_revision = ? AND body = ?
            """,
            (
                PromptAnnotationStatus.SENT.value,
                message_id,
                turn_id,
                sent_order,
                updated_at,
                annotation.annotation_id,
                PromptAnnotationStatus.DRAFT.value,
                annotation.state_revision,
                annotation.body,
            ),
        )
        try:
            if cursor.rowcount != 1:
                raise ArtifactConflictError("prompt annotation compare-and-swap failed")
        finally:
            await cursor.close()
        consumed.append(await get_prompt_annotation_on_conn(conn, annotation.annotation_id))
    return tuple(consumed)


def _edit_session_from_row(row: Any) -> EditSession:
    data = dict(row)
    data["mode"] = EditSessionMode(data["mode"])
    data["status"] = EditSessionStatus(data["status"])
    return EditSession(**data)


def _writer_lease_from_row(row: Any) -> WriterLease:
    return WriterLease(**dict(row))


def _audit_event_from_row(row: Any) -> AuditEvent:
    data = dict(row)
    data["actor_kind"] = ActorKind(data["actor_kind"])
    data["payload"] = _json_object(data.pop("payload_json"))
    return AuditEvent(**data)


class ArtifactSessionRepository:
    """Persist ArtifactSession records with one transaction per public operation.

    Production callers should use :meth:`from_session_storage` so this repository
    shares SessionStorage's connection, operation lock, busy budget, cancellation
    cleanup, and poisoned-connection handling. :meth:`open` exists for isolated
    tools and tests and owns the connection it creates.
    """

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

    def allocate_id(self, prefix: str) -> str:
        """Allocate an opaque id for a value later fenced by a transaction."""

        return self._id_factory(prefix)

    @classmethod
    async def from_session_storage(
        cls,
        storage: _SessionStorageBinding,
        *,
        clock: Clock = _now_ms,
        id_factory: IdFactory = _new_id,
    ) -> ArtifactSessionRepository:
        """Bind to the already-connected canonical SessionStorage transaction gate."""

        def transaction(operation: str) -> AbstractAsyncContextManager[Any]:
            return storage._write_transaction(f"artifact_session.{operation}")

        def read_transaction(operation: str) -> AbstractAsyncContextManager[Any]:
            return storage.read_transaction(f"artifact_session.{operation}")

        repository = cls(
            transaction,
            read_transaction_factory=read_transaction,
            clock=clock,
            id_factory=id_factory,
        )
        state = _storage_initialization_state(storage)
        async with state.lock:
            generation = storage.connection_generation
            if state.generation != generation:
                await repository.initialize()
                state.generation = generation
        return repository

    @classmethod
    async def open(
        cls,
        db_path: str | Path = ":memory:",
        *,
        clock: Clock = _now_ms,
        id_factory: IdFactory = _new_id,
    ) -> ArtifactSessionRepository:
        """Open an isolated SQLite repository, primarily for tests and local tools."""

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

        repository = cls(
            transaction,
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

    async def __aenter__(self) -> ArtifactSessionRepository:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close only a connection created by :meth:`open`."""

        if self._closed:
            return
        self._closed = True
        if self._owned_connection is not None:
            await self._owned_connection.close()

    def _transaction(self, operation: str) -> AbstractAsyncContextManager[Any]:
        if self._closed:
            raise RuntimeError("ArtifactSessionRepository is closed")
        return self._transaction_factory(operation)

    def _read_transaction(self, operation: str) -> AbstractAsyncContextManager[Any]:
        if self._closed:
            raise RuntimeError("ArtifactSessionRepository is closed")
        return self._read_transaction_factory(operation)

    async def initialize(self) -> None:
        """Idempotently reconcile the additive ArtifactSession schema."""

        async with self._transaction("initialize") as conn:
            for statement in SCHEMA_STATEMENTS:
                await conn.execute(statement)

    async def _get_document_on_conn(self, conn: Any, document_id: str) -> Document:
        row = await _fetchone(
            conn,
            "SELECT * FROM artifact_documents WHERE document_id = ?",
            (document_id,),
        )
        if row is None:
            raise ArtifactNotFoundError(f"document not found: {document_id}")
        return _document_from_row(row)

    async def _get_revision_on_conn(self, conn: Any, revision_id: str) -> Revision:
        row = await _fetchone(
            conn,
            "SELECT * FROM artifact_revisions WHERE revision_id = ?",
            (revision_id,),
        )
        if row is None:
            raise ArtifactNotFoundError(f"revision not found: {revision_id}")
        return _revision_from_row(row)

    async def _get_change_set_on_conn(self, conn: Any, change_set_id: str) -> ChangeSet:
        row = await _fetchone(
            conn,
            "SELECT * FROM artifact_change_sets WHERE change_set_id = ?",
            (change_set_id,),
        )
        if row is None:
            raise ArtifactNotFoundError(f"change set not found: {change_set_id}")
        return _change_set_from_row(row)

    async def _get_anchor_on_conn(self, conn: Any, anchor_id: str) -> Anchor:
        row = await _fetchone(
            conn,
            "SELECT * FROM artifact_anchors WHERE anchor_id = ?",
            (anchor_id,),
        )
        if row is None:
            raise ArtifactNotFoundError(f"anchor not found: {anchor_id}")
        return _anchor_from_row(row)

    async def _get_prompt_annotation_on_conn(
        self,
        conn: Any,
        annotation_id: str,
    ) -> PromptAnnotation:
        return await get_prompt_annotation_on_conn(conn, annotation_id)

    async def _get_mutation_attempt_on_conn(
        self,
        conn: Any,
        *,
        document_id: str,
        turn_id: str,
    ) -> MutationAttempt:
        row = await _fetchone(
            conn,
            """
            SELECT * FROM artifact_mutation_attempts
            WHERE document_id = ? AND turn_id = ?
            """,
            (document_id, turn_id),
        )
        if row is None:
            raise ArtifactNotFoundError(
                f"mutation attempt not found for document {document_id} and turn {turn_id}"
            )
        return _mutation_attempt_from_row(row)

    async def _get_document_source_binding_on_conn(
        self,
        conn: Any,
        binding_id: str,
    ) -> DocumentSourceBinding:
        row = await _fetchone(
            conn,
            "SELECT * FROM document_source_bindings WHERE binding_id = ?",
            (binding_id,),
        )
        if row is None:
            raise ArtifactNotFoundError(f"document source binding not found: {binding_id}")
        return _document_source_binding_from_row(row)

    async def _get_document_import_attempt_on_conn(
        self,
        conn: Any,
        *,
        session_id: str,
        idempotency_key: str,
    ) -> DocumentImportAttempt:
        row = await _fetchone(
            conn,
            """
            SELECT * FROM document_import_attempts
            WHERE session_id = ? AND idempotency_key = ?
            """,
            (session_id, idempotency_key),
        )
        if row is None:
            raise ArtifactNotFoundError(
                f"document import attempt not found for session {session_id}"
            )
        return _document_import_attempt_from_row(row)

    async def _get_document_publication_on_conn(
        self,
        conn: Any,
        publication_id: str,
    ) -> DocumentPublication:
        row = await _fetchone(
            conn,
            "SELECT * FROM document_publications WHERE publication_id = ?",
            (publication_id,),
        )
        if row is None:
            raise ArtifactNotFoundError(f"document publication not found: {publication_id}")
        return _document_publication_from_row(row)

    async def _get_document_publish_attempt_on_conn(
        self,
        conn: Any,
        *,
        session_id: str,
        idempotency_key: str,
    ) -> DocumentPublishAttempt:
        row = await _fetchone(
            conn,
            """
            SELECT * FROM document_publish_attempts
            WHERE session_id = ? AND idempotency_key = ?
            """,
            (session_id, idempotency_key),
        )
        if row is None:
            raise ArtifactNotFoundError(
                f"document publish attempt not found for session {session_id}"
            )
        return _document_publish_attempt_from_row(row)

    async def _get_edit_session_on_conn(self, conn: Any, edit_session_id: str) -> EditSession:
        row = await _fetchone(
            conn,
            "SELECT * FROM artifact_edit_sessions WHERE edit_session_id = ?",
            (edit_session_id,),
        )
        if row is None:
            raise ArtifactNotFoundError(f"edit session not found: {edit_session_id}")
        return _edit_session_from_row(row)

    async def _append_audit(
        self,
        conn: Any,
        *,
        document_id: str,
        event_type: str,
        actor: Actor,
        revision_id: str | None = None,
        change_set_id: str | None = None,
        anchor_id: str | None = None,
        edit_session_id: str | None = None,
        lease_id: str | None = None,
        payload: dict[str, Any] | None = None,
        created_at: int | None = None,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO artifact_audit_events (
                event_id, document_id, event_type, actor_kind, actor_id,
                revision_id, change_set_id, anchor_id,
                edit_session_id, lease_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._id_factory("audit"),
                document_id,
                event_type,
                actor.kind.value,
                actor.actor_id,
                revision_id,
                change_set_id,
                anchor_id,
                edit_session_id,
                lease_id,
                _json_dumps(payload or {}),
                self._clock() if created_at is None else created_at,
            ),
        )

    async def create_document(
        self,
        *,
        session_key: str,
        session_id: str | None,
        name: str,
        kind: ArtifactKind,
        initial_artifact: ArtifactBlobRef,
        actor: Actor,
        document_id: str | None = None,
        revision_id: str | None = None,
    ) -> CommitResult:
        """Create a document and its generation-one immutable snapshot atomically."""

        document_id = document_id or self._id_factory("doc")
        revision_id = revision_id or self._id_factory("rev")
        created_at = self._clock()
        async with self._transaction("create_document") as conn:
            return await self._create_document_on_conn(
                conn,
                session_key=session_key,
                session_id=session_id,
                name=name,
                kind=kind,
                initial_artifact=initial_artifact,
                actor=actor,
                document_id=document_id,
                revision_id=revision_id,
                created_at=created_at,
            )

    async def _create_document_on_conn(
        self,
        conn: Any,
        *,
        session_key: str,
        session_id: str | None,
        name: str,
        kind: ArtifactKind,
        initial_artifact: ArtifactBlobRef,
        actor: Actor,
        document_id: str,
        revision_id: str,
        created_at: int,
    ) -> CommitResult:
        await conn.execute(
            """
            INSERT INTO artifact_documents (
                document_id, session_key, session_id, name, kind,
                head_revision_id, generation, state_revision,
                writer_fencing_token, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, 0, ?, ?)
            """,
            (
                document_id,
                session_key,
                session_id,
                name,
                kind.value,
                revision_id,
                created_at,
                created_at,
            ),
        )
        await conn.execute(
            """
            INSERT INTO artifact_revisions (
                revision_id, document_id, parent_revision_id, generation,
                artifact_id, artifact_sha256, filename, media_type, byte_size,
                source, actor_kind, actor_id, change_set_id,
                copied_from_revision_id, created_at
            ) VALUES (?, ?, NULL, 1, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                revision_id,
                document_id,
                initial_artifact.artifact_id,
                initial_artifact.sha256,
                initial_artifact.filename,
                initial_artifact.media_type,
                initial_artifact.byte_size,
                RevisionSource.INITIAL.value,
                actor.kind.value,
                actor.actor_id,
                created_at,
            ),
        )
        await self._append_audit(
            conn,
            document_id=document_id,
            event_type="document.created",
            actor=actor,
            revision_id=revision_id,
            payload={"generation": 1, "kind": kind.value},
            created_at=created_at,
        )
        document = await self._get_document_on_conn(conn, document_id)
        revision = await self._get_revision_on_conn(conn, revision_id)
        return CommitResult(document=document, revision=revision)

    async def adopt_document(
        self,
        *,
        session_key: str,
        session_id: str,
        name: str,
        kind: ArtifactKind,
        initial_artifact: ArtifactBlobRef,
        actor: Actor,
    ) -> tuple[CommitResult, bool]:
        """Atomically return or create the document owning one session artifact."""

        async with self._transaction("adopt_document") as conn:
            rows = await _fetchall(
                conn,
                """
                SELECT DISTINCT document.document_id
                FROM artifact_documents AS document
                JOIN artifact_revisions AS revision
                  ON revision.document_id = document.document_id
                WHERE document.session_key = ?
                  AND document.session_id = ?
                  AND revision.artifact_id = ?
                ORDER BY document.document_id
                LIMIT 2
                """,
                (session_key, session_id, initial_artifact.artifact_id),
            )
            if len(rows) > 1:
                raise ArtifactConflictError("artifact is already adopted by multiple documents")
            if rows:
                document = await self._get_document_on_conn(
                    conn,
                    str(rows[0]["document_id"]),
                )
                revision = await self._get_revision_on_conn(
                    conn,
                    document.head_revision_id,
                )
                if revision.document_id != document.document_id:
                    raise ArtifactValidationError("document head belongs to another document")
                return CommitResult(document=document, revision=revision), False

            created = await self._create_document_on_conn(
                conn,
                session_key=session_key,
                session_id=session_id,
                name=name,
                kind=kind,
                initial_artifact=initial_artifact,
                actor=actor,
                document_id=self._id_factory("doc"),
                revision_id=self._id_factory("rev"),
                created_at=self._clock(),
            )
            return created, True

    async def adopt_generated_deliverable(
        self,
        *,
        session_key: str,
        session_id: str,
        name: str,
        kind: ArtifactKind,
        deliverable: ArtifactBlobRef,
        actor: Actor,
    ) -> tuple[CommitResult, DocumentSourceBinding, bool]:
        """Atomically adopt and bind one public generated deliverable.

        ``created`` reports whether this call created the source binding.  A
        legacy Document that already references the same immutable artifact is
        reused and bound instead of being duplicated.
        """

        async with self._transaction("adopt_generated_deliverable") as conn:
            binding_row = await _fetchone(
                conn,
                """
                SELECT * FROM document_source_bindings
                WHERE session_id = ? AND source_type = 'deliverable'
                  AND source_resource_id = ?
                """,
                (session_id, deliverable.artifact_id),
            )
            if binding_row is not None:
                binding = _document_source_binding_from_row(binding_row)
                expected_source = (
                    session_key,
                    deliverable.sha256,
                    deliverable.filename,
                    deliverable.media_type,
                    deliverable.byte_size,
                    DocumentImportMode.COPY,
                )
                actual_source = (
                    binding.session_key,
                    binding.source_sha256,
                    binding.source_name,
                    binding.source_mime,
                    binding.source_size,
                    binding.mode,
                )
                if actual_source != expected_source:
                    raise ArtifactConflictError(
                        "generated deliverable source binding changed"
                    )
                document = await self._get_document_on_conn(conn, binding.document_id)
                if document.session_key != session_key or document.session_id != session_id:
                    raise ArtifactConflictError(
                        "generated deliverable document scope changed"
                    )
                revision = await self._get_revision_on_conn(
                    conn,
                    document.head_revision_id,
                )
                if revision.document_id != document.document_id:
                    raise ArtifactValidationError(
                        "document head belongs to another document"
                    )
                return CommitResult(document=document, revision=revision), binding, False

            rows = await _fetchall(
                conn,
                """
                SELECT DISTINCT document.document_id
                FROM artifact_documents AS document
                JOIN artifact_revisions AS revision
                  ON revision.document_id = document.document_id
                WHERE document.session_key = ?
                  AND document.session_id = ?
                  AND revision.artifact_id = ?
                ORDER BY document.document_id
                LIMIT 2
                """,
                (session_key, session_id, deliverable.artifact_id),
            )
            if len(rows) > 1:
                raise ArtifactConflictError(
                    "generated deliverable is already adopted by multiple documents"
                )
            if rows:
                document = await self._get_document_on_conn(
                    conn,
                    str(rows[0]["document_id"]),
                )
                revision = await self._get_revision_on_conn(
                    conn,
                    document.head_revision_id,
                )
                commit = CommitResult(document=document, revision=revision)
            else:
                commit = await self._create_document_on_conn(
                    conn,
                    session_key=session_key,
                    session_id=session_id,
                    name=name,
                    kind=kind,
                    initial_artifact=deliverable,
                    actor=actor,
                    document_id=self._id_factory("doc"),
                    revision_id=self._id_factory("rev"),
                    created_at=self._clock(),
                )

            prior_document_binding = await _fetchone(
                conn,
                "SELECT binding_id FROM document_source_bindings WHERE document_id = ?",
                (commit.document.document_id,),
            )
            if prior_document_binding is not None:
                raise ArtifactConflictError(
                    "generated deliverable document already has another source binding"
                )
            binding_id = self._id_factory("binding")
            created_at = self._clock()
            await conn.execute(
                """
                INSERT INTO document_source_bindings (
                    binding_id, document_id, session_key, session_id,
                    source_type, source_resource_id, source_sha256,
                    source_name, source_mime, source_size, mode, created_at
                ) VALUES (?, ?, ?, ?, 'deliverable', ?, ?, ?, ?, ?, 'copy', ?)
                """,
                (
                    binding_id,
                    commit.document.document_id,
                    session_key,
                    session_id,
                    deliverable.artifact_id,
                    deliverable.sha256,
                    deliverable.filename,
                    deliverable.media_type,
                    deliverable.byte_size,
                    created_at,
                ),
            )
            binding = await self._get_document_source_binding_on_conn(conn, binding_id)
            return commit, binding, True

    async def get_document(self, document_id: str) -> Document:
        async with self._read_transaction("get_document") as conn:
            return await self._get_document_on_conn(conn, document_id)

    async def get_document_head(
        self,
        document_id: str,
        *,
        expected_revision_id: str | None = None,
    ) -> CommitResult:
        """Read one document and its current head under one transaction snapshot."""

        async with self._read_transaction("get_document_head") as conn:
            document = await self._get_document_on_conn(conn, document_id)
            if (
                expected_revision_id is not None
                and document.head_revision_id != expected_revision_id
            ):
                raise ArtifactConflictError("document head revision changed")
            revision = await self._get_revision_on_conn(conn, document.head_revision_id)
            if revision.document_id != document.document_id:
                raise ArtifactValidationError("document head belongs to another document")
            return CommitResult(document=document, revision=revision)

    async def reserve_document_import_attempt(
        self,
        *,
        session_key: str,
        session_id: str,
        idempotency_key: str,
        source_type: DocumentSourceType,
        source_resource_id: str,
        source_sha256: str,
        source_name: str,
        source_mime: str,
        source_size: int,
        document_name: str,
        mode: DocumentImportMode,
        candidate_artifact_id: str,
        attempt_id: str | None = None,
    ) -> tuple[DocumentImportAttempt, bool]:
        """Reserve one import journal row before external bytes are copied."""

        async with self._transaction("reserve_document_import_attempt") as conn:
            row = await _fetchone(
                conn,
                """
                SELECT * FROM document_import_attempts
                WHERE session_id = ? AND idempotency_key = ?
                """,
                (session_id, idempotency_key),
            )
            if row is not None:
                existing = _document_import_attempt_from_row(row)
                requested = (
                    session_key,
                    source_type,
                    source_resource_id,
                    source_sha256,
                    source_name,
                    source_mime,
                    source_size,
                    document_name,
                    mode,
                )
                actual = (
                    existing.session_key,
                    existing.source_type,
                    existing.source_resource_id,
                    existing.source_sha256,
                    existing.source_name,
                    existing.source_mime,
                    existing.source_size,
                    existing.document_name,
                    existing.mode,
                )
                if actual != requested:
                    raise ArtifactConflictError(
                        "document import idempotency key was reused with different input"
                    )
                return existing, False

            now = self._clock()
            attempt_id = attempt_id or self._id_factory("import")
            await conn.execute(
                """
                INSERT INTO document_import_attempts (
                    attempt_id, session_key, session_id, idempotency_key,
                    source_type, source_resource_id, source_sha256,
                    source_name, source_mime, source_size, document_name, mode,
                    candidate_artifact_id, status, state_revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', 1, ?, ?)
                """,
                (
                    attempt_id,
                    session_key,
                    session_id,
                    idempotency_key,
                    source_type.value,
                    source_resource_id,
                    source_sha256,
                    source_name,
                    source_mime,
                    source_size,
                    document_name,
                    mode.value,
                    candidate_artifact_id,
                    now,
                    now,
                ),
            )
            return (
                await self._get_document_import_attempt_on_conn(
                    conn,
                    session_id=session_id,
                    idempotency_key=idempotency_key,
                ),
                True,
            )

    async def get_document_import_attempt(
        self,
        *,
        session_id: str,
        idempotency_key: str,
    ) -> DocumentImportAttempt:
        async with self._transaction("get_document_import_attempt") as conn:
            return await self._get_document_import_attempt_on_conn(
                conn,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )

    async def list_document_import_attempts_for_recovery(
        self,
        *,
        limit: int = 100,
        after_attempt_id: str | None = None,
    ) -> tuple[DocumentImportAttempt, ...]:
        """List restart-recoverable imports and journaled unused candidates."""

        async with self._transaction("list_document_import_attempts_for_recovery") as conn:
            after_clause = "" if after_attempt_id is None else "AND attempt.attempt_id > ?"
            params: tuple[Any, ...] = (
                (limit,) if after_attempt_id is None else (after_attempt_id, limit)
            )
            rows = await _fetchall(
                conn,
                f"""
                SELECT attempt.*
                FROM document_import_attempts AS attempt
                LEFT JOIN artifact_revisions AS revision
                  ON revision.revision_id = attempt.revision_id
                WHERE (
                    attempt.status = 'reserved'
                    OR (
                        attempt.status = 'applied'
                        AND attempt.candidate_cleaned_at IS NULL
                        AND (
                            revision.artifact_id IS NULL
                            OR revision.artifact_id != attempt.candidate_artifact_id
                        )
                    )
                )
                {after_clause}
                ORDER BY attempt.attempt_id
                LIMIT ?
                """,
                params,
            )
            return tuple(_document_import_attempt_from_row(row) for row in rows)

    async def mark_document_import_candidate_cleaned(
        self,
        *,
        session_id: str,
        idempotency_key: str,
    ) -> DocumentImportAttempt:
        """Durably acknowledge removal of a copied candidate unused by a binding replay."""

        async with self._transaction("mark_document_import_candidate_cleaned") as conn:
            attempt = await self._get_document_import_attempt_on_conn(
                conn,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )
            if attempt.status is not MutationAttemptStatus.APPLIED:
                raise ArtifactConflictError("document import attempt has not been applied")
            if attempt.candidate_cleaned_at is not None:
                return attempt
            now = self._clock()
            cursor = await conn.execute(
                """
                UPDATE document_import_attempts
                SET candidate_cleaned_at = ?, state_revision = state_revision + 1,
                    updated_at = ?
                WHERE attempt_id = ? AND status = 'applied' AND candidate_cleaned_at IS NULL
                """,
                (now, now, attempt.attempt_id),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError(
                        "document import cleanup receipt compare-and-swap failed"
                    )
            finally:
                await cursor.close()
            return await self._get_document_import_attempt_on_conn(
                conn,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )

    async def get_document_source_binding(
        self,
        binding_id: str,
    ) -> DocumentSourceBinding:
        async with self._read_transaction("get_document_source_binding") as conn:
            return await self._get_document_source_binding_on_conn(conn, binding_id)

    async def get_document_source_binding_for_resource(
        self,
        *,
        session_id: str,
        source_type: DocumentSourceType,
        source_resource_id: str,
    ) -> DocumentSourceBinding | None:
        async with self._read_transaction("get_document_source_binding_for_resource") as conn:
            row = await _fetchone(
                conn,
                """
                SELECT * FROM document_source_bindings
                WHERE session_id = ? AND source_type = ? AND source_resource_id = ?
                """,
                (session_id, source_type.value, source_resource_id),
            )
            return None if row is None else _document_source_binding_from_row(row)

    async def list_document_source_bindings(
        self,
        *,
        session_id: str,
        limit: int = 500,
    ) -> tuple[DocumentSourceBinding, ...]:
        async with self._read_transaction("list_document_source_bindings") as conn:
            rows = await _fetchall(
                conn,
                """
                SELECT * FROM document_source_bindings
                WHERE session_id = ?
                ORDER BY created_at DESC, binding_id
                LIMIT ?
                """,
                (session_id, limit),
            )
            return tuple(_document_source_binding_from_row(row) for row in rows)

    async def apply_document_import_attempt(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        candidate_artifact: ArtifactBlobRef,
        document_name: str,
        kind: ArtifactKind,
        actor: Actor,
    ) -> DocumentImportResult:
        """Atomically create/reuse a document, bind its source, and receipt the import."""

        async with self._transaction("apply_document_import_attempt") as conn:
            attempt = await self._get_document_import_attempt_on_conn(
                conn,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )
            if attempt.status is MutationAttemptStatus.APPLIED:
                assert attempt.document_id and attempt.revision_id and attempt.binding_id
                document = await self._get_document_on_conn(conn, attempt.document_id)
                revision = await self._get_revision_on_conn(conn, attempt.revision_id)
                applied_binding = await self._get_document_source_binding_on_conn(
                    conn,
                    attempt.binding_id,
                )
                return DocumentImportResult(
                    attempt=attempt,
                    binding=applied_binding,
                    commit=CommitResult(document=document, revision=revision),
                )
            if attempt.status is not MutationAttemptStatus.RESERVED:
                raise ArtifactConflictError(
                    f"document import attempt is terminal: {attempt.status.value}"
                )
            if document_name != attempt.document_name:
                raise ArtifactValidationError("document import name does not match journal")
            expected_candidate = (
                attempt.candidate_artifact_id,
                attempt.source_sha256,
                attempt.document_name,
                attempt.source_mime,
                attempt.source_size,
            )
            actual_candidate = (
                candidate_artifact.artifact_id,
                candidate_artifact.sha256,
                candidate_artifact.filename,
                candidate_artifact.media_type,
                candidate_artifact.byte_size,
            )
            if actual_candidate != expected_candidate:
                raise ArtifactValidationError("document import candidate does not match journal")

            binding_row = await _fetchone(
                conn,
                """
                SELECT * FROM document_source_bindings
                WHERE session_id = ? AND source_type = ? AND source_resource_id = ?
                """,
                (session_id, attempt.source_type.value, attempt.source_resource_id),
            )
            commit: CommitResult
            binding: DocumentSourceBinding
            if binding_row is not None:
                binding = _document_source_binding_from_row(binding_row)
                if (
                    binding.session_key != attempt.session_key
                    or binding.source_sha256 != attempt.source_sha256
                    or binding.mode is not DocumentImportMode.COPY
                ):
                    raise ArtifactConflictError("document source binding changed")
                document = await self._get_document_on_conn(conn, binding.document_id)
                revision = await self._get_revision_on_conn(conn, document.head_revision_id)
                commit = CommitResult(document=document, revision=revision)
            else:
                commit = await self._create_document_on_conn(
                    conn,
                    session_key=attempt.session_key,
                    session_id=attempt.session_id,
                    name=attempt.document_name,
                    kind=kind,
                    initial_artifact=candidate_artifact,
                    actor=actor,
                    document_id=self._id_factory("doc"),
                    revision_id=self._id_factory("rev"),
                    created_at=self._clock(),
                )
                binding_id = self._id_factory("binding")
                created_at = self._clock()
                await conn.execute(
                    """
                    INSERT INTO document_source_bindings (
                        binding_id, document_id, session_key, session_id,
                        source_type, source_resource_id, source_sha256,
                        source_name, source_mime, source_size, mode, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        binding_id,
                        commit.document.document_id,
                        attempt.session_key,
                        attempt.session_id,
                        attempt.source_type.value,
                        attempt.source_resource_id,
                        attempt.source_sha256,
                        attempt.source_name,
                        attempt.source_mime,
                        attempt.source_size,
                        attempt.mode.value,
                        created_at,
                    ),
                )
                binding = await self._get_document_source_binding_on_conn(conn, binding_id)

            now = self._clock()
            cursor = await conn.execute(
                """
                UPDATE document_import_attempts
                SET status = 'applied', document_id = ?, revision_id = ?, binding_id = ?,
                    failure_code = NULL, state_revision = state_revision + 1, updated_at = ?
                WHERE attempt_id = ? AND status = 'reserved'
                """,
                (
                    commit.document.document_id,
                    commit.revision.revision_id,
                    binding.binding_id,
                    now,
                    attempt.attempt_id,
                ),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError("document import receipt compare-and-swap failed")
            finally:
                await cursor.close()
            applied = await self._get_document_import_attempt_on_conn(
                conn,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )
            return DocumentImportResult(attempt=applied, binding=binding, commit=commit)

    async def fail_document_import_attempt(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        failure_code: str,
        ambiguous: bool = False,
    ) -> DocumentImportAttempt:
        async with self._transaction("fail_document_import_attempt") as conn:
            attempt = await self._get_document_import_attempt_on_conn(
                conn,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )
            if attempt.status is not MutationAttemptStatus.RESERVED:
                return attempt
            await conn.execute(
                """
                UPDATE document_import_attempts
                SET status = ?, failure_code = ?, state_revision = state_revision + 1,
                    updated_at = ?
                WHERE attempt_id = ? AND status = 'reserved'
                """,
                (
                    "ambiguous" if ambiguous else "failed",
                    failure_code,
                    self._clock(),
                    attempt.attempt_id,
                ),
            )
            return await self._get_document_import_attempt_on_conn(
                conn,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )

    async def reserve_document_publish_attempt(
        self,
        *,
        session_key: str,
        session_id: str,
        idempotency_key: str,
        document_id: str,
        revision_id: str,
        candidate_artifact: ArtifactBlobRef,
        attempt_id: str | None = None,
    ) -> tuple[DocumentPublishAttempt, bool]:
        """Reserve one publication journal row before the listed copy is exposed."""

        async with self._transaction("reserve_document_publish_attempt") as conn:
            document = await self._get_document_on_conn(conn, document_id)
            if document.session_key != session_key or document.session_id != session_id:
                raise ArtifactNotFoundError(f"document not found: {document_id}")
            revision = await self._get_revision_on_conn(conn, revision_id)
            if revision.document_id != document_id:
                raise ArtifactNotFoundError(f"revision not found: {revision_id}")
            if (
                revision.artifact_sha256 != candidate_artifact.sha256
                or revision.byte_size != candidate_artifact.byte_size
            ):
                raise ArtifactValidationError("publication candidate does not match revision")

            row = await _fetchone(
                conn,
                """
                SELECT * FROM document_publish_attempts
                WHERE session_id = ? AND idempotency_key = ?
                """,
                (session_id, idempotency_key),
            )
            if row is not None:
                existing = _document_publish_attempt_from_row(row)
                requested = (
                    session_key,
                    document_id,
                    revision_id,
                    candidate_artifact.sha256,
                    candidate_artifact.filename,
                    candidate_artifact.media_type,
                    candidate_artifact.byte_size,
                )
                actual = (
                    existing.session_key,
                    existing.document_id,
                    existing.revision_id,
                    existing.artifact_sha256,
                    existing.name,
                    existing.mime,
                    existing.size,
                )
                if actual != requested:
                    raise ArtifactConflictError(
                        "document publish idempotency key was reused with different input"
                    )
                return existing, False

            now = self._clock()
            attempt_id = attempt_id or self._id_factory("publish")
            await conn.execute(
                """
                INSERT INTO document_publish_attempts (
                    attempt_id, session_key, session_id, idempotency_key,
                    document_id, revision_id, candidate_artifact_id,
                    artifact_sha256, name, mime, size, status,
                    state_revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', 1, ?, ?)
                """,
                (
                    attempt_id,
                    session_key,
                    session_id,
                    idempotency_key,
                    document_id,
                    revision_id,
                    candidate_artifact.artifact_id,
                    candidate_artifact.sha256,
                    candidate_artifact.filename,
                    candidate_artifact.media_type,
                    candidate_artifact.byte_size,
                    now,
                    now,
                ),
            )
            return (
                await self._get_document_publish_attempt_on_conn(
                    conn,
                    session_id=session_id,
                    idempotency_key=idempotency_key,
                ),
                True,
            )

    async def get_document_publish_attempt(
        self,
        *,
        session_id: str,
        idempotency_key: str,
    ) -> DocumentPublishAttempt:
        async with self._transaction("get_document_publish_attempt") as conn:
            return await self._get_document_publish_attempt_on_conn(
                conn,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )

    async def list_document_publish_attempts_for_recovery(
        self,
        *,
        limit: int = 100,
        after_attempt_id: str | None = None,
    ) -> tuple[DocumentPublishAttempt, ...]:
        """List reserved writes and applied publications needing idempotent promotion."""

        async with self._transaction("list_document_publish_attempts_for_recovery") as conn:
            after_clause = "" if after_attempt_id is None else "AND attempt_id > ?"
            params: tuple[Any, ...] = (
                (limit,) if after_attempt_id is None else (after_attempt_id, limit)
            )
            rows = await _fetchall(
                conn,
                f"""
                SELECT * FROM document_publish_attempts
                WHERE (
                    status = 'reserved'
                    OR (status = 'applied' AND promoted_at IS NULL)
                )
                {after_clause}
                ORDER BY attempt_id
                LIMIT ?
                """,
                params,
            )
            return tuple(_document_publish_attempt_from_row(row) for row in rows)

    async def mark_document_publish_promoted(
        self,
        *,
        session_id: str,
        idempotency_key: str,
    ) -> DocumentPublishAttempt:
        """Durably acknowledge that a committed publication is externally visible."""

        async with self._transaction("mark_document_publish_promoted") as conn:
            attempt = await self._get_document_publish_attempt_on_conn(
                conn,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )
            if attempt.status is not MutationAttemptStatus.APPLIED:
                raise ArtifactConflictError("document publish attempt has not been applied")
            if attempt.promoted_at is not None:
                return attempt
            now = self._clock()
            cursor = await conn.execute(
                """
                UPDATE document_publish_attempts
                SET promoted_at = ?, state_revision = state_revision + 1, updated_at = ?
                WHERE attempt_id = ? AND status = 'applied' AND promoted_at IS NULL
                """,
                (now, now, attempt.attempt_id),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError(
                        "document publish promotion receipt compare-and-swap failed"
                    )
            finally:
                await cursor.close()
            return await self._get_document_publish_attempt_on_conn(
                conn,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )

    async def apply_document_publish_attempt(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        actor: Actor,
    ) -> DocumentPublishResult:
        """Atomically persist a revision-pinned immutable publication receipt."""

        async with self._transaction("apply_document_publish_attempt") as conn:
            attempt = await self._get_document_publish_attempt_on_conn(
                conn,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )
            if attempt.status is MutationAttemptStatus.APPLIED:
                assert attempt.publication_id is not None
                publication = await self._get_document_publication_on_conn(
                    conn,
                    attempt.publication_id,
                )
                return DocumentPublishResult(attempt=attempt, publication=publication)
            if attempt.status is not MutationAttemptStatus.RESERVED:
                raise ArtifactConflictError(
                    f"document publish attempt is terminal: {attempt.status.value}"
                )
            document = await self._get_document_on_conn(conn, attempt.document_id)
            if (
                document.session_key != attempt.session_key
                or document.session_id != attempt.session_id
            ):
                raise ArtifactNotFoundError(f"document not found: {attempt.document_id}")
            revision = await self._get_revision_on_conn(conn, attempt.revision_id)
            if revision.document_id != document.document_id:
                raise ArtifactNotFoundError(f"revision not found: {attempt.revision_id}")
            if (
                revision.artifact_sha256 != attempt.artifact_sha256
                or revision.byte_size != attempt.size
            ):
                raise ArtifactConflictError("document revision changed during publication")

            publication_id = self._id_factory("publication")
            created_at = self._clock()
            await conn.execute(
                """
                INSERT INTO document_publications (
                    publication_id, session_key, session_id, document_id, revision_id,
                    deliverable_artifact_id, artifact_sha256, name, mime, size,
                    created_by_kind, created_by_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication_id,
                    attempt.session_key,
                    attempt.session_id,
                    attempt.document_id,
                    attempt.revision_id,
                    attempt.candidate_artifact_id,
                    attempt.artifact_sha256,
                    attempt.name,
                    attempt.mime,
                    attempt.size,
                    actor.kind.value,
                    actor.actor_id,
                    created_at,
                ),
            )
            cursor = await conn.execute(
                """
                UPDATE document_publish_attempts
                SET status = 'applied', publication_id = ?, deliverable_artifact_id = ?,
                    failure_code = NULL, state_revision = state_revision + 1, updated_at = ?
                WHERE attempt_id = ? AND status = 'reserved'
                """,
                (
                    publication_id,
                    attempt.candidate_artifact_id,
                    created_at,
                    attempt.attempt_id,
                ),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError("document publish receipt compare-and-swap failed")
            finally:
                await cursor.close()
            await self._append_audit(
                conn,
                document_id=document.document_id,
                event_type="document.published",
                actor=actor,
                revision_id=revision.revision_id,
                payload={
                    "publication_id": publication_id,
                    "deliverable_artifact_id": attempt.candidate_artifact_id,
                },
                created_at=created_at,
            )
            applied = await self._get_document_publish_attempt_on_conn(
                conn,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )
            publication = await self._get_document_publication_on_conn(conn, publication_id)
            return DocumentPublishResult(attempt=applied, publication=publication)

    async def fail_document_publish_attempt(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        failure_code: str,
        ambiguous: bool = False,
    ) -> DocumentPublishAttempt:
        async with self._transaction("fail_document_publish_attempt") as conn:
            attempt = await self._get_document_publish_attempt_on_conn(
                conn,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )
            if attempt.status is not MutationAttemptStatus.RESERVED:
                return attempt
            await conn.execute(
                """
                UPDATE document_publish_attempts
                SET status = ?, failure_code = ?, state_revision = state_revision + 1,
                    updated_at = ?
                WHERE attempt_id = ? AND status = 'reserved'
                """,
                (
                    "ambiguous" if ambiguous else "failed",
                    failure_code,
                    self._clock(),
                    attempt.attempt_id,
                ),
            )
            return await self._get_document_publish_attempt_on_conn(
                conn,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )

    async def get_document_publication(
        self,
        publication_id: str,
    ) -> DocumentPublication:
        async with self._read_transaction("get_document_publication") as conn:
            return await self._get_document_publication_on_conn(conn, publication_id)

    async def list_document_publications(
        self,
        *,
        session_id: str,
        document_id: str | None = None,
        limit: int = 500,
    ) -> tuple[DocumentPublication, ...]:
        async with self._read_transaction("list_document_publications") as conn:
            where = "session_id = ?"
            params: tuple[Any, ...] = (session_id, limit)
            if document_id is not None:
                where += " AND document_id = ?"
                params = (session_id, document_id, limit)
            rows = await _fetchall(
                conn,
                f"""
                SELECT * FROM document_publications
                WHERE {where}
                ORDER BY created_at DESC, publication_id
                LIMIT ?
                """,
                params,
            )
            return tuple(_document_publication_from_row(row) for row in rows)

    async def rename_document(
        self,
        *,
        document_id: str,
        expected_state_revision: int,
        name: str,
        actor: Actor,
    ) -> Document:
        """Rename a document without changing its immutable revision head."""

        now = self._clock()
        async with self._transaction("rename_document") as conn:
            document = await self._get_document_on_conn(conn, document_id)
            if document.state_revision != expected_state_revision:
                raise ArtifactConflictError("document state_revision changed")
            cursor = await conn.execute(
                """
                UPDATE artifact_documents
                SET name = ?, state_revision = state_revision + 1, updated_at = ?
                WHERE document_id = ? AND state_revision = ?
                """,
                (name, now, document_id, expected_state_revision),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError("document rename compare-and-swap failed")
            finally:
                await cursor.close()
            await self._append_audit(
                conn,
                document_id=document_id,
                event_type="document.renamed",
                actor=actor,
                revision_id=document.head_revision_id,
                payload={"old_name": document.name, "new_name": name},
                created_at=now,
            )
            return await self._get_document_on_conn(conn, document_id)

    async def list_documents(
        self,
        *,
        session_key: str,
        session_id: str | None = None,
        limit: int = 100,
    ) -> tuple[Document, ...]:
        async with self._read_transaction("list_documents") as conn:
            where = "session_key = ?"
            params: tuple[Any, ...] = (session_key, limit)
            if session_id is not None:
                where += " AND session_id = ?"
                params = (session_key, session_id, limit)
            rows = await _fetchall(
                conn,
                f"""
                SELECT * FROM artifact_documents
                WHERE {where}
                ORDER BY updated_at DESC, document_id
                LIMIT ?
                """,
                params,
            )
            return tuple(_document_from_row(row) for row in rows)

    async def snapshot_session_heads(
        self,
        *,
        session_id: str,
    ) -> tuple[CommitResult, ...]:
        """Return a stable description of every current head in one session epoch."""

        async with self._transaction("snapshot_session_heads") as conn:
            rows = await _fetchall(
                conn,
                """
                SELECT * FROM artifact_documents
                WHERE session_id = ?
                ORDER BY document_id
                """,
                (session_id,),
            )
            snapshots: list[CommitResult] = []
            for row in rows:
                document = _document_from_row(row)
                revision = await self._get_revision_on_conn(conn, document.head_revision_id)
                if revision.document_id != document.document_id:
                    raise ArtifactValidationError("document head belongs to another document")
                snapshots.append(CommitResult(document=document, revision=revision))
            return tuple(snapshots)

    async def fork_session_heads(
        self,
        *,
        source_session_id: str,
        target_session_key: str,
        target_session_id: str,
        snapshots: Sequence[CommitResult],
        actor: Actor,
    ) -> tuple[CommitResult, ...]:
        """Create generation-one child documents from an exact source-head snapshot.

        Only document metadata and the current immutable head are copied. Annotations,
        anchors, change sets, leases, and edit sessions deliberately remain in the
        parent. Every source head is revalidated in the write transaction so a fork
        cannot silently mix bytes from one revision with metadata from another.
        """

        if source_session_id == target_session_id:
            raise ArtifactValidationError("source and target session ids must differ")
        async with self._transaction("fork_session_heads") as conn:
            results: list[CommitResult] = []
            seen_documents: set[str] = set()
            for snapshot in snapshots:
                source_document = snapshot.document
                source_revision = snapshot.revision
                if source_document.document_id in seen_documents:
                    raise ArtifactValidationError("fork snapshot contains a duplicate document")
                seen_documents.add(source_document.document_id)
                current_document = await self._get_document_on_conn(
                    conn,
                    source_document.document_id,
                )
                if (
                    current_document.session_id != source_session_id
                    or current_document.head_revision_id != source_revision.revision_id
                    or current_document.state_revision != source_document.state_revision
                ):
                    raise ArtifactConflictError("artifact head changed while session was forked")
                current_revision = await self._get_revision_on_conn(
                    conn,
                    current_document.head_revision_id,
                )
                if current_revision.document_id != current_document.document_id:
                    raise ArtifactValidationError("document head belongs to another document")

                document_id = self._id_factory("doc")
                revision_id = self._id_factory("rev")
                created_at = self._clock()
                await conn.execute(
                    """
                    INSERT INTO artifact_documents (
                        document_id, session_key, session_id, name, kind,
                        head_revision_id, generation, state_revision,
                        writer_fencing_token, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, 0, ?, ?)
                    """,
                    (
                        document_id,
                        target_session_key,
                        target_session_id,
                        current_document.name,
                        current_document.kind.value,
                        revision_id,
                        created_at,
                        created_at,
                    ),
                )
                await conn.execute(
                    """
                    INSERT INTO artifact_revisions (
                        revision_id, document_id, parent_revision_id, generation,
                        artifact_id, artifact_sha256, filename, media_type, byte_size,
                        source, actor_kind, actor_id, change_set_id,
                        copied_from_revision_id, created_at
                    ) VALUES (?, ?, NULL, 1, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        revision_id,
                        document_id,
                        current_revision.artifact_id,
                        current_revision.artifact_sha256,
                        current_revision.filename,
                        current_revision.media_type,
                        current_revision.byte_size,
                        RevisionSource.INITIAL.value,
                        actor.kind.value,
                        actor.actor_id,
                        current_revision.revision_id,
                        created_at,
                    ),
                )
                await self._append_audit(
                    conn,
                    document_id=document_id,
                    event_type="document.forked",
                    actor=actor,
                    revision_id=revision_id,
                    payload={
                        "source_document_id": current_document.document_id,
                        "copied_from_revision_id": current_revision.revision_id,
                        "source_session_id": source_session_id,
                    },
                    created_at=created_at,
                )
                document = await self._get_document_on_conn(conn, document_id)
                revision = await self._get_revision_on_conn(conn, revision_id)
                results.append(CommitResult(document=document, revision=revision))
            return tuple(results)

    async def get_revision(self, revision_id: str) -> Revision:
        async with self._read_transaction("get_revision") as conn:
            return await self._get_revision_on_conn(conn, revision_id)

    async def list_revisions(
        self,
        document_id: str,
        *,
        limit: int = 100,
    ) -> tuple[Revision, ...]:
        async with self._transaction("list_revisions") as conn:
            await self._get_document_on_conn(conn, document_id)
            rows = await _fetchall(
                conn,
                """
                SELECT * FROM artifact_revisions
                WHERE document_id = ?
                ORDER BY generation DESC
                LIMIT ?
                """,
                (document_id, limit),
            )
            return tuple(_revision_from_row(row) for row in rows)

    async def _validate_writer_lease(
        self,
        conn: Any,
        *,
        document_id: str,
        lease_id: str,
        fencing_token: int,
        now: int,
    ) -> WriterLease:
        row = await _fetchone(
            conn,
            "SELECT * FROM artifact_writer_leases WHERE document_id = ?",
            (document_id,),
        )
        if row is None:
            raise WriterLeaseExpiredError("document has no active writer lease")
        lease = _writer_lease_from_row(row)
        if lease.lease_id != lease_id or lease.fencing_token != fencing_token:
            raise WriterLeaseExpiredError("writer lease fencing token is stale")
        if lease.expires_at <= now:
            raise WriterLeaseExpiredError("writer lease has expired")
        return lease

    async def _commit_revision_on_conn(
        self,
        conn: Any,
        *,
        document_id: str,
        expected_head_revision_id: str,
        expected_state_revision: int,
        artifact: ArtifactBlobRef,
        actor: Actor,
        source: RevisionSource,
        change_set_id: str | None = None,
        copied_from_revision_id: str | None = None,
        lease_id: str | None = None,
        fencing_token: int | None = None,
        require_lease: bool = False,
        event_type: str = "revision.committed",
        revision_id: str | None = None,
    ) -> CommitResult:
        now = self._clock()
        document = await self._get_document_on_conn(conn, document_id)
        if (
            document.head_revision_id != expected_head_revision_id
            or document.state_revision != expected_state_revision
        ):
            raise ArtifactConflictError(
                "document head changed; refresh head_revision_id and state_revision"
            )
        if require_lease and (lease_id is None or fencing_token is None):
            raise WriterLeaseExpiredError("a live writer lease is required")
        if lease_id is not None or fencing_token is not None:
            if lease_id is None or fencing_token is None:
                raise ArtifactValidationError(
                    "lease_id and fencing_token must be supplied together"
                )
            await self._validate_writer_lease(
                conn,
                document_id=document_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
                now=now,
            )
        if copied_from_revision_id is not None:
            copied = await self._get_revision_on_conn(conn, copied_from_revision_id)
            if copied.document_id != document_id:
                raise ArtifactValidationError("copied revision belongs to another document")

        revision_id = revision_id or self._id_factory("rev")
        generation = document.generation + 1
        await conn.execute(
            """
            INSERT INTO artifact_revisions (
                revision_id, document_id, parent_revision_id, generation,
                artifact_id, artifact_sha256, filename, media_type, byte_size,
                source, actor_kind, actor_id, change_set_id,
                copied_from_revision_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                document_id,
                document.head_revision_id,
                generation,
                artifact.artifact_id,
                artifact.sha256,
                artifact.filename,
                artifact.media_type,
                artifact.byte_size,
                source.value,
                actor.kind.value,
                actor.actor_id,
                change_set_id,
                copied_from_revision_id,
                now,
            ),
        )
        cursor = await conn.execute(
            """
            UPDATE artifact_documents
            SET head_revision_id = ?, generation = ?,
                state_revision = state_revision + 1, updated_at = ?
            WHERE document_id = ?
              AND head_revision_id = ?
              AND state_revision = ?
            """,
            (
                revision_id,
                generation,
                now,
                document_id,
                expected_head_revision_id,
                expected_state_revision,
            ),
        )
        try:
            if cursor.rowcount != 1:
                raise ArtifactConflictError("document head compare-and-swap failed")
        finally:
            await cursor.close()
        await self._append_audit(
            conn,
            document_id=document_id,
            event_type=event_type,
            actor=actor,
            revision_id=revision_id,
            change_set_id=change_set_id,
            lease_id=lease_id,
            payload={
                "generation": generation,
                "parent_revision_id": document.head_revision_id,
                "copied_from_revision_id": copied_from_revision_id,
                "source": source.value,
                "fencing_token": fencing_token,
            },
            created_at=now,
        )
        updated = await self._get_document_on_conn(conn, document_id)
        revision = await self._get_revision_on_conn(conn, revision_id)
        return CommitResult(document=updated, revision=revision)

    async def commit_revision(
        self,
        *,
        document_id: str,
        expected_head_revision_id: str,
        expected_state_revision: int,
        artifact: ArtifactBlobRef,
        actor: Actor,
        source: RevisionSource = RevisionSource.MANUAL,
        lease_id: str | None = None,
        fencing_token: int | None = None,
        require_lease: bool = False,
    ) -> CommitResult:
        """Advance head only when both caller head expectations still match."""

        if source in {RevisionSource.INITIAL, RevisionSource.RESTORE, RevisionSource.REVERT}:
            raise ArtifactValidationError("use the dedicated create/restore/revert operation")
        async with self._transaction("commit_revision") as conn:
            return await self._commit_revision_on_conn(
                conn,
                document_id=document_id,
                expected_head_revision_id=expected_head_revision_id,
                expected_state_revision=expected_state_revision,
                artifact=artifact,
                actor=actor,
                source=source,
                lease_id=lease_id,
                fencing_token=fencing_token,
                require_lease=require_lease,
            )

    async def _copy_revision_as_new_head(
        self,
        *,
        operation: str,
        event_type: str,
        source: RevisionSource,
        document_id: str,
        target_revision_id: str,
        expected_head_revision_id: str,
        expected_state_revision: int,
        actor: Actor,
        lease_id: str | None,
        fencing_token: int | None,
        require_lease: bool,
    ) -> CommitResult:
        async with self._transaction(operation) as conn:
            target = await self._get_revision_on_conn(conn, target_revision_id)
            if target.document_id != document_id:
                raise ArtifactValidationError("target revision belongs to another document")
            return await self._commit_revision_on_conn(
                conn,
                document_id=document_id,
                expected_head_revision_id=expected_head_revision_id,
                expected_state_revision=expected_state_revision,
                artifact=target.artifact,
                actor=actor,
                source=source,
                copied_from_revision_id=target_revision_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
                require_lease=require_lease,
                event_type=event_type,
            )

    async def restore_revision(
        self,
        *,
        document_id: str,
        target_revision_id: str,
        expected_head_revision_id: str,
        expected_state_revision: int,
        actor: Actor,
        lease_id: str | None = None,
        fencing_token: int | None = None,
        require_lease: bool = False,
    ) -> CommitResult:
        """Restore bytes by appending a new revision; never move head backwards."""

        return await self._copy_revision_as_new_head(
            operation="restore_revision",
            event_type="document.restored",
            source=RevisionSource.RESTORE,
            document_id=document_id,
            target_revision_id=target_revision_id,
            expected_head_revision_id=expected_head_revision_id,
            expected_state_revision=expected_state_revision,
            actor=actor,
            lease_id=lease_id,
            fencing_token=fencing_token,
            require_lease=require_lease,
        )

    async def revert_revision(
        self,
        *,
        document_id: str,
        target_revision_id: str,
        expected_head_revision_id: str,
        expected_state_revision: int,
        actor: Actor,
        lease_id: str | None = None,
        fencing_token: int | None = None,
        require_lease: bool = False,
    ) -> CommitResult:
        """Revert to a snapshot by appending a new revision with explicit provenance."""

        return await self._copy_revision_as_new_head(
            operation="revert_revision",
            event_type="document.reverted",
            source=RevisionSource.REVERT,
            document_id=document_id,
            target_revision_id=target_revision_id,
            expected_head_revision_id=expected_head_revision_id,
            expected_state_revision=expected_state_revision,
            actor=actor,
            lease_id=lease_id,
            fencing_token=fencing_token,
            require_lease=require_lease,
        )

    async def acquire_writer_lease(
        self,
        *,
        document_id: str,
        holder_id: str,
        ttl_ms: int,
        actor: Actor,
    ) -> WriterLease:
        """Acquire exclusive ownership and allocate the next monotonic fence."""

        now = self._clock()
        async with self._transaction("acquire_writer_lease") as conn:
            document = await self._get_document_on_conn(conn, document_id)
            row = await _fetchone(
                conn,
                "SELECT * FROM artifact_writer_leases WHERE document_id = ?",
                (document_id,),
            )
            if row is not None:
                current = _writer_lease_from_row(row)
                if current.expires_at > now:
                    if current.holder_id == holder_id:
                        return current
                    raise WriterLeaseConflictError("another writer owns the document lease")
                await conn.execute(
                    "DELETE FROM artifact_writer_leases WHERE document_id = ?",
                    (document_id,),
                )
            token = document.writer_fencing_token + 1
            lease_id = self._id_factory("lease")
            expires_at = now + ttl_ms
            cursor = await conn.execute(
                """
                UPDATE artifact_documents
                SET writer_fencing_token = ?
                WHERE document_id = ? AND writer_fencing_token = ?
                """,
                (token, document_id, document.writer_fencing_token),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError("writer fencing token compare-and-swap failed")
            finally:
                await cursor.close()
            await conn.execute(
                """
                INSERT INTO artifact_writer_leases (
                    document_id, lease_id, holder_id, fencing_token,
                    expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (document_id, lease_id, holder_id, token, expires_at, now, now),
            )
            await self._append_audit(
                conn,
                document_id=document_id,
                event_type="writer_lease.acquired",
                actor=actor,
                lease_id=lease_id,
                payload={
                    "holder_id": holder_id,
                    "fencing_token": token,
                    "expires_at": expires_at,
                },
                created_at=now,
            )
            row = await _fetchone(
                conn,
                "SELECT * FROM artifact_writer_leases WHERE document_id = ?",
                (document_id,),
            )
            assert row is not None
            return _writer_lease_from_row(row)

    async def get_writer_lease(self, document_id: str) -> WriterLease | None:
        async with self._transaction("get_writer_lease") as conn:
            await self._get_document_on_conn(conn, document_id)
            row = await _fetchone(
                conn,
                "SELECT * FROM artifact_writer_leases WHERE document_id = ?",
                (document_id,),
            )
            if row is None:
                return None
            lease = _writer_lease_from_row(row)
            return lease if lease.expires_at > self._clock() else None

    async def renew_writer_lease(
        self,
        *,
        document_id: str,
        lease_id: str,
        fencing_token: int,
        ttl_ms: int,
        actor: Actor,
    ) -> WriterLease:
        now = self._clock()
        async with self._transaction("renew_writer_lease") as conn:
            lease = await self._validate_writer_lease(
                conn,
                document_id=document_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
                now=now,
            )
            expires_at = now + ttl_ms
            await conn.execute(
                """
                UPDATE artifact_writer_leases
                SET expires_at = ?, updated_at = ?
                WHERE document_id = ? AND lease_id = ? AND fencing_token = ?
                """,
                (expires_at, now, document_id, lease_id, fencing_token),
            )
            await self._append_audit(
                conn,
                document_id=document_id,
                event_type="writer_lease.renewed",
                actor=actor,
                lease_id=lease_id,
                payload={"fencing_token": fencing_token, "expires_at": expires_at},
                created_at=now,
            )
            return WriterLease(
                lease_id=lease.lease_id,
                document_id=lease.document_id,
                holder_id=lease.holder_id,
                fencing_token=lease.fencing_token,
                expires_at=expires_at,
                created_at=lease.created_at,
                updated_at=now,
                schema_version=lease.schema_version,
            )

    async def release_writer_lease(
        self,
        *,
        document_id: str,
        lease_id: str,
        fencing_token: int,
        actor: Actor,
    ) -> None:
        now = self._clock()
        async with self._transaction("release_writer_lease") as conn:
            await self._validate_writer_lease(
                conn,
                document_id=document_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
                now=now,
            )
            await conn.execute(
                """
                DELETE FROM artifact_writer_leases
                WHERE document_id = ? AND lease_id = ? AND fencing_token = ?
                """,
                (document_id, lease_id, fencing_token),
            )
            await self._append_audit(
                conn,
                document_id=document_id,
                event_type="writer_lease.released",
                actor=actor,
                lease_id=lease_id,
                payload={"fencing_token": fencing_token},
                created_at=now,
            )

    async def create_change_set(
        self,
        *,
        document_id: str,
        base_revision_id: str,
        operations: Sequence[dict[str, Any]],
        actor: Actor,
        turn_id: str | None = None,
        summary: str = "",
        change_set_id: str | None = None,
    ) -> ChangeSet:
        """Persist an agent or user change set against an immutable base revision."""

        change_set_id = change_set_id or self._id_factory("change")
        now = self._clock()
        async with self._transaction("create_change_set") as conn:
            await self._get_document_on_conn(conn, document_id)
            base = await self._get_revision_on_conn(conn, base_revision_id)
            if base.document_id != document_id:
                raise ArtifactValidationError("base revision belongs to another document")
            try:
                await conn.execute(
                    """
                    INSERT INTO artifact_change_sets (
                        change_set_id, document_id, base_revision_id, turn_id,
                        summary, status,
                        operations_json, state_revision, created_by_kind,
                        created_by_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        change_set_id,
                        document_id,
                        base_revision_id,
                        turn_id,
                        summary,
                        ChangeSetStatus.DRAFT.value,
                        _json_dumps(list(operations)),
                        actor.kind.value,
                        actor.actor_id,
                        now,
                        now,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                if turn_id is not None:
                    raise ArtifactConflictError(
                        "this agent turn already has a persistent change set"
                    ) from exc
                raise
            await self._append_audit(
                conn,
                document_id=document_id,
                event_type="change_set.created",
                actor=actor,
                change_set_id=change_set_id,
                payload={
                    "base_revision_id": base_revision_id,
                    "operation_count": len(operations),
                    "turn_id": turn_id,
                },
                created_at=now,
            )
            return await self._get_change_set_on_conn(conn, change_set_id)

    async def get_change_set(self, change_set_id: str) -> ChangeSet:
        async with self._transaction("get_change_set") as conn:
            return await self._get_change_set_on_conn(conn, change_set_id)

    async def get_change_set_by_turn(
        self,
        *,
        document_id: str,
        turn_id: str,
    ) -> ChangeSet | None:
        """Load the sole persistent change set for a document turn, if any."""

        async with self._transaction("get_change_set_by_turn") as conn:
            await self._get_document_on_conn(conn, document_id)
            row = await _fetchone(
                conn,
                """
                SELECT * FROM artifact_change_sets
                WHERE document_id = ? AND turn_id = ?
                """,
                (document_id, turn_id),
            )
            return None if row is None else _change_set_from_row(row)

    async def list_change_sets(
        self,
        document_id: str,
        *,
        status: ChangeSetStatus | None = None,
        limit: int = 100,
    ) -> tuple[ChangeSet, ...]:
        async with self._transaction("list_change_sets") as conn:
            await self._get_document_on_conn(conn, document_id)
            if status is None:
                rows = await _fetchall(
                    conn,
                    """
                    SELECT * FROM artifact_change_sets
                    WHERE document_id = ?
                    ORDER BY updated_at DESC, change_set_id
                    LIMIT ?
                    """,
                    (document_id, limit),
                )
            else:
                rows = await _fetchall(
                    conn,
                    """
                    SELECT * FROM artifact_change_sets
                    WHERE document_id = ? AND status = ?
                    ORDER BY updated_at DESC, change_set_id
                    LIMIT ?
                    """,
                    (document_id, status.value, limit),
                )
            return tuple(_change_set_from_row(row) for row in rows)

    async def ready_change_set(
        self,
        *,
        change_set_id: str,
        expected_state_revision: int,
        candidate_artifact: ArtifactBlobRef,
        validation: dict[str, Any] | None,
        actor: Actor,
    ) -> ChangeSet:
        """Attach validated candidate bytes and transition a draft to ready."""

        now = self._clock()
        async with self._transaction("ready_change_set") as conn:
            change_set = await self._get_change_set_on_conn(conn, change_set_id)
            if change_set.state_revision != expected_state_revision:
                raise ArtifactConflictError("change set state_revision changed")
            if change_set.status is not ChangeSetStatus.DRAFT:
                raise ArtifactConflictError("only a draft change set can become ready")
            cursor = await conn.execute(
                """
                UPDATE artifact_change_sets
                SET status = ?, candidate_artifact_id = ?,
                    candidate_artifact_sha256 = ?, candidate_filename = ?,
                    candidate_media_type = ?, candidate_byte_size = ?,
                    validation_json = ?, state_revision = state_revision + 1,
                    updated_at = ?
                WHERE change_set_id = ? AND state_revision = ? AND status = ?
                """,
                (
                    ChangeSetStatus.READY.value,
                    candidate_artifact.artifact_id,
                    candidate_artifact.sha256,
                    candidate_artifact.filename,
                    candidate_artifact.media_type,
                    candidate_artifact.byte_size,
                    None if validation is None else _json_dumps(validation),
                    now,
                    change_set_id,
                    expected_state_revision,
                    ChangeSetStatus.DRAFT.value,
                ),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError("change set compare-and-swap failed")
            finally:
                await cursor.close()
            await self._append_audit(
                conn,
                document_id=change_set.document_id,
                event_type="change_set.ready",
                actor=actor,
                change_set_id=change_set_id,
                payload={"base_revision_id": change_set.base_revision_id},
                created_at=now,
            )
            return await self._get_change_set_on_conn(conn, change_set_id)

    async def reject_change_set(
        self,
        *,
        change_set_id: str,
        expected_state_revision: int,
        actor: Actor,
        reason: str | None = None,
    ) -> ChangeSet:
        now = self._clock()
        async with self._transaction("reject_change_set") as conn:
            change_set = await self._get_change_set_on_conn(conn, change_set_id)
            if change_set.state_revision != expected_state_revision:
                raise ArtifactConflictError("change set state_revision changed")
            if change_set.status not in {
                ChangeSetStatus.DRAFT,
                ChangeSetStatus.READY,
                ChangeSetStatus.CONFLICT,
                ChangeSetStatus.FAILED,
            }:
                raise ArtifactConflictError("change set is already terminal")
            cursor = await conn.execute(
                """
                UPDATE artifact_change_sets
                SET status = ?, state_revision = state_revision + 1, updated_at = ?
                WHERE change_set_id = ? AND state_revision = ?
                """,
                (
                    ChangeSetStatus.REJECTED.value,
                    now,
                    change_set_id,
                    expected_state_revision,
                ),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError("change set compare-and-swap failed")
            finally:
                await cursor.close()
            await self._append_audit(
                conn,
                document_id=change_set.document_id,
                event_type="change_set.rejected",
                actor=actor,
                change_set_id=change_set_id,
                payload={"reason": reason},
                created_at=now,
            )
            return await self._get_change_set_on_conn(conn, change_set_id)

    async def apply_change_set(
        self,
        *,
        change_set_id: str,
        expected_change_set_state_revision: int,
        expected_head_revision_id: str,
        expected_document_state_revision: int,
        actor: Actor,
        lease_id: str | None = None,
        fencing_token: int | None = None,
        require_lease: bool = False,
    ) -> CommitResult:
        """Atomically apply ready candidate bytes and mark the change set applied."""

        async with self._transaction("apply_change_set") as conn:
            change_set = await self._get_change_set_on_conn(conn, change_set_id)
            if change_set.state_revision != expected_change_set_state_revision:
                raise ArtifactConflictError("change set state_revision changed")
            if change_set.status is not ChangeSetStatus.READY:
                raise ArtifactConflictError("change set is not ready")
            if change_set.base_revision_id != expected_head_revision_id:
                raise ArtifactConflictError("change set base is no longer document head")
            candidate = change_set.candidate_artifact
            if candidate is None:
                raise ArtifactValidationError("ready change set has no complete candidate artifact")
            result = await self._commit_revision_on_conn(
                conn,
                document_id=change_set.document_id,
                expected_head_revision_id=expected_head_revision_id,
                expected_state_revision=expected_document_state_revision,
                artifact=candidate,
                actor=actor,
                source=RevisionSource.AGENT,
                change_set_id=change_set_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
                require_lease=require_lease,
                event_type="revision.change_set_applied",
            )
            now = self._clock()
            cursor = await conn.execute(
                """
                UPDATE artifact_change_sets
                SET status = ?, applied_revision_id = ?,
                    state_revision = state_revision + 1, updated_at = ?
                WHERE change_set_id = ? AND state_revision = ? AND status = ?
                """,
                (
                    ChangeSetStatus.APPLIED.value,
                    result.revision.revision_id,
                    now,
                    change_set_id,
                    expected_change_set_state_revision,
                    ChangeSetStatus.READY.value,
                ),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError("change set compare-and-swap failed")
            finally:
                await cursor.close()
            await self._append_audit(
                conn,
                document_id=change_set.document_id,
                event_type="change_set.applied",
                actor=actor,
                revision_id=result.revision.revision_id,
                change_set_id=change_set_id,
                payload={"base_revision_id": change_set.base_revision_id},
                created_at=now,
            )
            return result

    async def commit_change_set_atomically(
        self,
        *,
        document_id: str,
        base_revision_id: str,
        expected_document_state_revision: int,
        operations: Sequence[dict[str, Any]],
        candidate_artifact: ArtifactBlobRef,
        validation: dict[str, Any] | None,
        actor: Actor,
        turn_id: str,
        summary: str = "",
        change_set_id: str | None = None,
        source: RevisionSource = RevisionSource.AGENT,
        copied_from_revision_id: str | None = None,
        revision_event_type: str = "revision.change_set_applied",
        lease_id: str | None = None,
        fencing_token: int | None = None,
        require_lease: bool = False,
        edit_session_id: str | None = None,
        expected_edit_session_state_revision: int | None = None,
        expected_last_saved_revision_id: str | None = None,
    ) -> tuple[CommitResult, ChangeSet]:
        """Create and apply one change set in a single SQLite transaction.

        The change set, head revision, document CAS, and audit rows commit as one
        unit. Any validation, lease, CAS, or persistence fault rolls everything
        back, leaving neither a revision nor a proposal row to clean up.
        """

        change_set_id = change_set_id or self._id_factory("change")
        now = self._clock()
        async with self._transaction("commit_change_set_atomically") as conn:
            document = await self._get_document_on_conn(conn, document_id)
            base = await self._get_revision_on_conn(conn, base_revision_id)
            if base.document_id != document_id:
                raise ArtifactValidationError("base revision belongs to another document")
            if document.head_revision_id != base_revision_id:
                raise ArtifactConflictError("change set base is no longer document head")
            if document.state_revision != expected_document_state_revision:
                raise ArtifactConflictError("document state_revision changed")
            edit_session_fields = (
                edit_session_id,
                expected_edit_session_state_revision,
                expected_last_saved_revision_id,
            )
            if any(value is not None for value in edit_session_fields) and not all(
                value is not None for value in edit_session_fields
            ):
                raise ArtifactValidationError(
                    "edit session id, state revision, and saved revision must be supplied together"
                )
            edit_session: EditSession | None = None
            if edit_session_id is not None:
                assert expected_edit_session_state_revision is not None
                assert expected_last_saved_revision_id is not None
                edit_session = await self._get_edit_session_on_conn(conn, edit_session_id)
                if edit_session.document_id != document_id:
                    raise ArtifactValidationError("edit session belongs to another document")
                if edit_session.user_id != actor.actor_id:
                    raise ArtifactConflictError("edit session belongs to another user")
                if edit_session.mode is not EditSessionMode.EDIT:
                    raise ArtifactConflictError("edit session is read-only")
                if edit_session.status is not EditSessionStatus.ACTIVE:
                    raise ArtifactConflictError("edit session is not active")
                if edit_session.expires_at <= now:
                    raise ArtifactConflictError("edit session has expired")
                if (
                    edit_session.state_revision != expected_edit_session_state_revision
                    or edit_session.last_saved_revision_id != expected_last_saved_revision_id
                ):
                    raise ArtifactConflictError("edit session save position changed")
                if expected_last_saved_revision_id != base_revision_id:
                    raise ArtifactConflictError("edit session is not based on the document head")
            try:
                await conn.execute(
                    """
                    INSERT INTO artifact_change_sets (
                        change_set_id, document_id, base_revision_id, turn_id,
                        summary, status, operations_json,
                        candidate_artifact_id, candidate_artifact_sha256,
                        candidate_filename, candidate_media_type, candidate_byte_size,
                        validation_json, state_revision, created_by_kind,
                        created_by_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2, ?, ?, ?, ?)
                    """,
                    (
                        change_set_id,
                        document_id,
                        base_revision_id,
                        turn_id,
                        summary,
                        ChangeSetStatus.READY.value,
                        _json_dumps(list(operations)),
                        candidate_artifact.artifact_id,
                        candidate_artifact.sha256,
                        candidate_artifact.filename,
                        candidate_artifact.media_type,
                        candidate_artifact.byte_size,
                        None if validation is None else _json_dumps(validation),
                        actor.kind.value,
                        actor.actor_id,
                        now,
                        now,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                raise ArtifactConflictError(
                    "this agent turn already has a persistent change set"
                ) from exc
            await self._append_audit(
                conn,
                document_id=document_id,
                event_type="change_set.created",
                actor=actor,
                change_set_id=change_set_id,
                payload={
                    "base_revision_id": base_revision_id,
                    "operation_count": len(operations),
                    "turn_id": turn_id,
                },
                created_at=now,
            )
            await self._append_audit(
                conn,
                document_id=document_id,
                event_type="change_set.ready",
                actor=actor,
                change_set_id=change_set_id,
                payload={"base_revision_id": base_revision_id},
                created_at=now,
            )
            result = await self._commit_revision_on_conn(
                conn,
                document_id=document_id,
                expected_head_revision_id=base_revision_id,
                expected_state_revision=expected_document_state_revision,
                artifact=candidate_artifact,
                actor=actor,
                source=source,
                change_set_id=change_set_id,
                copied_from_revision_id=copied_from_revision_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
                require_lease=require_lease,
                event_type=revision_event_type,
            )
            applied_at = self._clock()
            cursor = await conn.execute(
                """
                UPDATE artifact_change_sets
                SET status = ?, applied_revision_id = ?,
                    state_revision = 3, updated_at = ?
                WHERE change_set_id = ? AND state_revision = 2 AND status = ?
                """,
                (
                    ChangeSetStatus.APPLIED.value,
                    result.revision.revision_id,
                    applied_at,
                    change_set_id,
                    ChangeSetStatus.READY.value,
                ),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError("change set compare-and-swap failed")
            finally:
                await cursor.close()
            await self._append_audit(
                conn,
                document_id=document_id,
                event_type="change_set.applied",
                actor=actor,
                revision_id=result.revision.revision_id,
                change_set_id=change_set_id,
                payload={"base_revision_id": base_revision_id},
                created_at=applied_at,
            )
            if edit_session is not None:
                assert expected_edit_session_state_revision is not None
                assert expected_last_saved_revision_id is not None
                cursor = await conn.execute(
                    """
                    UPDATE artifact_edit_sessions
                    SET last_saved_revision_id = ?, state_revision = state_revision + 1,
                        last_access_at = ?, updated_at = ?
                    WHERE edit_session_id = ? AND state_revision = ?
                      AND last_saved_revision_id = ? AND status = ?
                    """,
                    (
                        result.revision.revision_id,
                        applied_at,
                        applied_at,
                        edit_session.edit_session_id,
                        expected_edit_session_state_revision,
                        expected_last_saved_revision_id,
                        EditSessionStatus.ACTIVE.value,
                    ),
                )
                try:
                    if cursor.rowcount != 1:
                        raise ArtifactConflictError("edit session compare-and-swap failed")
                finally:
                    await cursor.close()
                await self._append_audit(
                    conn,
                    document_id=document_id,
                    event_type="edit_session.saved",
                    actor=actor,
                    revision_id=result.revision.revision_id,
                    edit_session_id=edit_session.edit_session_id,
                    lease_id=lease_id,
                    payload={"previous_revision_id": expected_last_saved_revision_id},
                    created_at=applied_at,
                )
            return result, await self._get_change_set_on_conn(conn, change_set_id)

    async def reserve_mutation_attempt_with_status(
        self,
        *,
        document_id: str,
        turn_id: str,
        tool_use_id: str,
        base_revision_id: str,
        proposal_sha256: str | None,
        mutation_attempt_id: str | None = None,
    ) -> tuple[MutationAttempt, bool]:
        """Reserve the sole artifact-writing slot for a turn.

        Replaying the same ``tool_use_id``, base revision, and non-null proposal
        digest returns the original receipt in any state. A different or missing
        digest fails closed before it can create a second persistent mutation.
        """

        mutation_attempt_id = mutation_attempt_id or self._id_factory("mutation")
        now = self._clock()
        async with self._transaction("reserve_mutation_attempt") as conn:
            row = await _fetchone(
                conn,
                """
                SELECT * FROM artifact_mutation_attempts
                WHERE turn_id = ?
                """,
                (turn_id,),
            )
            if row is not None:
                existing = _mutation_attempt_from_row(row)
                if (
                    existing.document_id == document_id
                    and existing.tool_use_id == tool_use_id
                    and existing.base_revision_id == base_revision_id
                    and existing.proposal_sha256 is not None
                    and proposal_sha256 is not None
                    and existing.proposal_sha256 == proposal_sha256
                ):
                    return existing, False
                raise ArtifactConflictError(
                    "this turn is reserved by a different mutation tool call or document"
                )

            document = await self._get_document_on_conn(conn, document_id)
            base = await self._get_revision_on_conn(conn, base_revision_id)
            if base.document_id != document_id:
                raise ArtifactValidationError("base revision belongs to another document")
            if document.head_revision_id != base_revision_id:
                raise ArtifactConflictError("mutation base is no longer document head")
            try:
                await conn.execute(
                    """
                    INSERT INTO artifact_mutation_attempts (
                        mutation_attempt_id, document_id, turn_id, tool_use_id,
                        base_revision_id, proposal_sha256, status,
                        state_revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        mutation_attempt_id,
                        document_id,
                        turn_id,
                        tool_use_id,
                        base_revision_id,
                        proposal_sha256,
                        MutationAttemptStatus.RESERVED.value,
                        now,
                        now,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                # BEGIN IMMEDIATE serializes normal repository writers, but keep
                # the unique constraint authoritative for foreign/manual callers.
                row = await _fetchone(
                    conn,
                    """
                    SELECT * FROM artifact_mutation_attempts
                    WHERE turn_id = ?
                    """,
                    (turn_id,),
                )
                if row is not None:
                    existing = _mutation_attempt_from_row(row)
                    if (
                        existing.document_id == document_id
                        and existing.tool_use_id == tool_use_id
                        and existing.base_revision_id == base_revision_id
                        and existing.proposal_sha256 is not None
                        and proposal_sha256 is not None
                        and existing.proposal_sha256 == proposal_sha256
                    ):
                        return existing, False
                    raise ArtifactConflictError(
                        "this turn is reserved by a different mutation tool call or document"
                    ) from exc
                raise ArtifactConflictError("mutation_attempt_id is already in use") from exc
            return (
                await self._get_mutation_attempt_on_conn(
                    conn,
                    document_id=document_id,
                    turn_id=turn_id,
                ),
                True,
            )

    async def reserve_mutation_attempt(
        self,
        *,
        document_id: str,
        turn_id: str,
        tool_use_id: str,
        base_revision_id: str,
        proposal_sha256: str | None,
        mutation_attempt_id: str | None = None,
    ) -> MutationAttempt:
        attempt, _created = await self.reserve_mutation_attempt_with_status(
            document_id=document_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            base_revision_id=base_revision_id,
            proposal_sha256=proposal_sha256,
            mutation_attempt_id=mutation_attempt_id,
        )
        return attempt

    async def register_mutation_candidate(
        self,
        *,
        document_id: str,
        turn_id: str,
        candidate_session_id: str,
        candidate_artifact_id: str,
        candidate_artifact_sha256: str,
    ) -> MutationAttempt:
        """Journal a preallocated candidate before any bytes are published."""

        now = self._clock()
        async with self._transaction("register_mutation_candidate") as conn:
            attempt = await self._get_mutation_attempt_on_conn(
                conn,
                document_id=document_id,
                turn_id=turn_id,
            )
            if attempt.status is not MutationAttemptStatus.RESERVED:
                raise ArtifactConflictError("mutation attempt is no longer reserved")
            document = await self._get_document_on_conn(conn, document_id)
            if document.session_id != candidate_session_id:
                raise ArtifactValidationError(
                    "candidate session does not match the artifact document"
                )
            requested = (
                candidate_session_id,
                candidate_artifact_id,
                candidate_artifact_sha256,
            )
            existing = (
                attempt.candidate_session_id,
                attempt.candidate_artifact_id,
                attempt.candidate_artifact_sha256,
            )
            if existing == requested:
                return attempt
            if any(value is not None for value in existing):
                raise ArtifactConflictError("mutation candidate is already registered")
            try:
                cursor = await conn.execute(
                    """
                    UPDATE artifact_mutation_attempts
                    SET candidate_session_id = ?, candidate_artifact_id = ?,
                        candidate_artifact_sha256 = ?, candidate_registered_at = ?,
                        state_revision = state_revision + 1, updated_at = ?
                    WHERE mutation_attempt_id = ? AND state_revision = ?
                      AND status = 'reserved' AND candidate_artifact_id IS NULL
                    """,
                    (
                        candidate_session_id,
                        candidate_artifact_id,
                        candidate_artifact_sha256,
                        now,
                        now,
                        attempt.mutation_attempt_id,
                        attempt.state_revision,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                raise ArtifactConflictError("mutation candidate id is already journaled") from exc
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError("mutation candidate compare-and-swap failed")
            finally:
                await cursor.close()
            return await self._get_mutation_attempt_on_conn(
                conn,
                document_id=document_id,
                turn_id=turn_id,
            )

    async def list_unresolved_mutation_attempts(
        self,
        *,
        limit: int = 100,
        after_mutation_attempt_id: str | None = None,
    ) -> tuple[MutationAttempt, ...]:
        """List restart-recoverable mutation receipts in deterministic order."""

        async with self._transaction("list_unresolved_mutation_attempts") as conn:
            where_after = "" if after_mutation_attempt_id is None else "AND mutation_attempt_id > ?"
            params: tuple[Any, ...] = (
                (limit,)
                if after_mutation_attempt_id is None
                else (after_mutation_attempt_id, limit)
            )
            rows = await _fetchall(
                conn,
                f"""
                SELECT * FROM artifact_mutation_attempts
                WHERE status IN ('reserved', 'ambiguous')
                {where_after}
                ORDER BY mutation_attempt_id
                LIMIT ?
                """,
                params,
            )
            return tuple(_mutation_attempt_from_row(row) for row in rows)

    async def list_mutation_attempts_by_turn_ids(
        self,
        *,
        session_key: str,
        turn_ids: Sequence[str],
    ) -> tuple[MutationAttempt, ...]:
        """Load exact mutation receipts without crossing the session boundary.

        ``turn_id`` is globally unique in the mutation table, but history is a
        session-scoped read surface.  Joining through the owning document makes
        that boundary authoritative even when a caller supplies a valid turn
        identifier from another session.
        """

        ordered_ids = tuple(dict.fromkeys(turn_ids))
        if not ordered_ids:
            return ()
        rows_by_turn_id: dict[str, MutationAttempt] = {}
        async with self._read_transaction("list_mutation_attempts_by_turn_ids") as conn:
            for index in range(0, len(ordered_ids), _MUTATION_ATTEMPT_TURN_QUERY_CHUNK_SIZE):
                chunk = ordered_ids[index : index + _MUTATION_ATTEMPT_TURN_QUERY_CHUNK_SIZE]
                placeholders = ", ".join("?" for _ in chunk)
                rows = await _fetchall(
                    conn,
                    f"""
                    SELECT attempt.*
                    FROM artifact_mutation_attempts AS attempt
                    JOIN artifact_documents AS document
                      ON document.document_id = attempt.document_id
                    WHERE document.session_key = ?
                      AND attempt.turn_id IN ({placeholders})
                    """,
                    (session_key, *chunk),
                )
                for row in rows:
                    attempt = _mutation_attempt_from_row(row)
                    rows_by_turn_id[attempt.turn_id] = attempt
        return tuple(
            rows_by_turn_id[turn_id] for turn_id in ordered_ids if turn_id in rows_by_turn_id
        )

    async def reconcile_mutation_attempt(
        self,
        *,
        document_id: str,
        turn_id: str,
        tool_use_id: str,
    ) -> MutationAttempt:
        """Return a mutation receipt only when it belongs to the same tool call."""

        async with self._transaction("reconcile_mutation_attempt") as conn:
            attempt = await self._get_mutation_attempt_on_conn(
                conn,
                document_id=document_id,
                turn_id=turn_id,
            )
            if attempt.tool_use_id != tool_use_id:
                raise ArtifactConflictError("mutation attempt belongs to a different tool_use_id")
            return attempt

    async def get_mutation_attempt_for_resolution(
        self,
        *,
        document_id: str,
        turn_id: str,
    ) -> MutationAttempt:
        """Load a receipt for a session-scoped product outcome query.

        Tool execution reconciliation continues to require ``tool_use_id`` via
        :meth:`reconcile_mutation_attempt`.  This narrower read exists for the
        Gateway's authenticated mutation-resolution RPC, which verifies the
        owning Document and never returns the receipt itself.
        """

        async with self._read_transaction("get_mutation_attempt_for_resolution") as conn:
            return await self._get_mutation_attempt_on_conn(
                conn,
                document_id=document_id,
                turn_id=turn_id,
            )

    async def _mutation_result_refs_on_conn(
        self,
        conn: Any,
        *,
        attempt: MutationAttempt,
        change_set_id: str | None,
        revision_id: str | None,
        require_applied: bool,
    ) -> None:
        change_set: ChangeSet | None = None
        revision: Revision | None = None
        if change_set_id is not None:
            change_set = await self._get_change_set_on_conn(conn, change_set_id)
            if change_set.document_id != attempt.document_id:
                raise ArtifactValidationError("change set belongs to another document")
            if change_set.turn_id != attempt.turn_id:
                raise ArtifactValidationError("change set belongs to another turn")
            if change_set.base_revision_id != attempt.base_revision_id:
                raise ArtifactValidationError("change set uses another base revision")
        if revision_id is not None:
            revision = await self._get_revision_on_conn(conn, revision_id)
            if revision.document_id != attempt.document_id:
                raise ArtifactValidationError("revision belongs to another document")
        if change_set is not None and revision is not None:
            if revision.change_set_id != change_set.change_set_id:
                raise ArtifactValidationError("revision was not produced by the change set")
        if require_applied:
            if change_set is None or revision is None:
                raise ArtifactValidationError(
                    "applied mutation requires change_set_id and revision_id"
                )
            if (
                change_set.status is not ChangeSetStatus.APPLIED
                or change_set.applied_revision_id != revision.revision_id
            ):
                raise ArtifactConflictError("change set has not applied the requested revision")
            if attempt.candidate_artifact_id is None:
                return
            assert change_set is not None and revision is not None
            if (
                change_set.candidate_artifact_id != attempt.candidate_artifact_id
                or change_set.candidate_artifact_sha256 != attempt.candidate_artifact_sha256
                or revision.artifact_id != attempt.candidate_artifact_id
                or revision.artifact_sha256 != attempt.candidate_artifact_sha256
            ):
                raise ArtifactConflictError(
                    "applied result does not match the journaled mutation candidate"
                )

    async def _mark_mutation_attempt(
        self,
        *,
        document_id: str,
        turn_id: str,
        tool_use_id: str,
        status: MutationAttemptStatus,
        change_set_id: str | None,
        revision_id: str | None,
        failure_code: str | None,
    ) -> MutationAttempt:
        now = self._clock()
        async with self._transaction(f"mark_mutation_attempt_{status.value}") as conn:
            attempt = await self._get_mutation_attempt_on_conn(
                conn,
                document_id=document_id,
                turn_id=turn_id,
            )
            if attempt.tool_use_id != tool_use_id:
                raise ArtifactConflictError("mutation attempt belongs to a different tool_use_id")
            requested = (status, change_set_id, revision_id, failure_code)
            existing = (
                attempt.status,
                attempt.change_set_id,
                attempt.revision_id,
                attempt.failure_code,
            )
            if existing == requested:
                return attempt
            if attempt.status not in {
                MutationAttemptStatus.RESERVED,
                MutationAttemptStatus.AMBIGUOUS,
            }:
                raise ArtifactConflictError("mutation attempt is already terminal")
            if (
                attempt.status is MutationAttemptStatus.AMBIGUOUS
                and status is MutationAttemptStatus.AMBIGUOUS
            ):
                raise ArtifactConflictError("ambiguous mutation receipt already differs")
            await self._mutation_result_refs_on_conn(
                conn,
                attempt=attempt,
                change_set_id=change_set_id,
                revision_id=revision_id,
                require_applied=status is MutationAttemptStatus.APPLIED,
            )
            cursor = await conn.execute(
                """
                UPDATE artifact_mutation_attempts
                SET status = ?, change_set_id = ?, revision_id = ?, failure_code = ?,
                    state_revision = state_revision + 1, updated_at = ?
                WHERE mutation_attempt_id = ? AND tool_use_id = ?
                  AND state_revision = ? AND status IN ('reserved', 'ambiguous')
                """,
                (
                    status.value,
                    change_set_id,
                    revision_id,
                    failure_code,
                    now,
                    attempt.mutation_attempt_id,
                    tool_use_id,
                    attempt.state_revision,
                ),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError("mutation attempt compare-and-swap failed")
            finally:
                await cursor.close()
            return await self._get_mutation_attempt_on_conn(
                conn,
                document_id=document_id,
                turn_id=turn_id,
            )

    async def mark_mutation_attempt_applied(
        self,
        *,
        document_id: str,
        turn_id: str,
        tool_use_id: str,
        change_set_id: str,
        revision_id: str,
    ) -> MutationAttempt:
        return await self._mark_mutation_attempt(
            document_id=document_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            status=MutationAttemptStatus.APPLIED,
            change_set_id=change_set_id,
            revision_id=revision_id,
            failure_code=None,
        )

    async def mark_mutation_attempt_failed(
        self,
        *,
        document_id: str,
        turn_id: str,
        tool_use_id: str,
        failure_code: str,
        change_set_id: str | None = None,
    ) -> MutationAttempt:
        return await self._mark_mutation_attempt(
            document_id=document_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            status=MutationAttemptStatus.FAILED,
            change_set_id=change_set_id,
            revision_id=None,
            failure_code=failure_code,
        )

    async def mark_mutation_attempt_ambiguous(
        self,
        *,
        document_id: str,
        turn_id: str,
        tool_use_id: str,
        failure_code: str,
        change_set_id: str | None = None,
        revision_id: str | None = None,
    ) -> MutationAttempt:
        return await self._mark_mutation_attempt(
            document_id=document_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            status=MutationAttemptStatus.AMBIGUOUS,
            change_set_id=change_set_id,
            revision_id=revision_id,
            failure_code=failure_code,
        )

    async def create_anchor(
        self,
        *,
        document_id: str,
        revision_id: str,
        kind: AnchorKind,
        locator: dict[str, Any],
        actor: Actor,
        quote: str | None = None,
        context: dict[str, Any] | None = None,
        state: AnchorState = AnchorState.RESOLVED,
        remapped_from_anchor_id: str | None = None,
        anchor_id: str | None = None,
    ) -> Anchor:
        """Append an immutable, revision-scoped anchor."""

        anchor_id = anchor_id or self._id_factory("anchor")
        now = self._clock()
        async with self._transaction("create_anchor") as conn:
            await self._get_document_on_conn(conn, document_id)
            revision = await self._get_revision_on_conn(conn, revision_id)
            if revision.document_id != document_id:
                raise ArtifactValidationError("anchor revision belongs to another document")
            if remapped_from_anchor_id is not None:
                old_anchor = await self._get_anchor_on_conn(conn, remapped_from_anchor_id)
                if old_anchor.document_id != document_id:
                    raise ArtifactValidationError("remapped anchor belongs to another document")
            await conn.execute(
                """
                INSERT INTO artifact_anchors (
                    anchor_id, document_id, revision_id, kind, locator_json,
                    quote, context_json, state, remapped_from_anchor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    anchor_id,
                    document_id,
                    revision_id,
                    kind.value,
                    _json_dumps(locator),
                    quote,
                    None if context is None else _json_dumps(context),
                    state.value,
                    remapped_from_anchor_id,
                    now,
                ),
            )
            await self._append_audit(
                conn,
                document_id=document_id,
                event_type="anchor.created",
                actor=actor,
                revision_id=revision_id,
                anchor_id=anchor_id,
                payload={
                    "kind": kind.value,
                    "remapped_from_anchor_id": remapped_from_anchor_id,
                },
                created_at=now,
            )
            return await self._get_anchor_on_conn(conn, anchor_id)

    async def get_anchor(self, anchor_id: str) -> Anchor:
        async with self._transaction("get_anchor") as conn:
            return await self._get_anchor_on_conn(conn, anchor_id)

    async def _insert_prompt_annotation_on_conn(
        self,
        conn: Any,
        *,
        annotation_id: str,
        session_key: str,
        session_id: str,
        session_epoch: int,
        document_id: str,
        revision_id: str,
        anchor_id: str,
        body: str,
        now: int,
    ) -> None:
        """Insert one draft on a caller-owned transaction connection."""

        await conn.execute(
            """
            INSERT INTO artifact_prompt_annotations (
                annotation_id, session_key, session_id, session_epoch,
                document_id, revision_id, anchor_id, body, status,
                state_revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                annotation_id,
                session_key,
                session_id,
                session_epoch,
                document_id,
                revision_id,
                anchor_id,
                body,
                PromptAnnotationStatus.DRAFT.value,
                now,
                now,
            ),
        )

    async def create_prompt_annotation_with_anchor(
        self,
        *,
        annotation_id: str,
        session_key: str,
        session_id: str,
        session_epoch: int,
        document_id: str,
        revision_id: str,
        kind: AnchorKind,
        locator: dict[str, Any],
        actor: Actor,
        quote: str | None,
        context: dict[str, Any] | None,
        body: str,
    ) -> tuple[Anchor, PromptAnnotation]:
        """Atomically create one immutable anchor and its idempotent draft."""

        now = self._clock()
        async with self._transaction("create_prompt_annotation_with_anchor") as conn:
            document = await self._get_document_on_conn(conn, document_id)
            if document.session_key != session_key or document.session_id != session_id:
                raise ArtifactNotFoundError(f"document not found: {document_id}")
            if document.head_revision_id != revision_id:
                raise ArtifactConflictError("prompt annotation revision is no longer current")
            revision = await self._get_revision_on_conn(conn, revision_id)
            if revision.document_id != document_id:
                raise ArtifactValidationError(
                    "prompt annotation revision belongs to another document"
                )

            existing_row = await _fetchone(
                conn,
                "SELECT * FROM artifact_prompt_annotations WHERE annotation_id = ?",
                (annotation_id,),
            )
            if existing_row is not None:
                existing = _prompt_annotation_from_row(existing_row)
                if (
                    existing.session_key != session_key
                    or existing.session_id != session_id
                    or existing.session_epoch != session_epoch
                ):
                    raise ArtifactNotFoundError(f"prompt annotation not found: {annotation_id}")
                anchor = await self._get_anchor_on_conn(conn, existing.anchor_id)
                if (
                    existing.status is PromptAnnotationStatus.DRAFT
                    and existing.document_id == document_id
                    and existing.revision_id == revision_id
                    and existing.body == body
                    and anchor.document_id == document_id
                    and anchor.revision_id == revision_id
                    and anchor.kind is kind
                    and anchor.locator == locator
                    and anchor.quote == quote
                    and anchor.context == context
                    and anchor.state is AnchorState.RESOLVED
                    and anchor.remapped_from_anchor_id is None
                ):
                    return anchor, existing
                raise ArtifactConflictError("prompt annotation id is already in use")

            count_row = await _fetchone(
                conn,
                """
                SELECT COUNT(*) AS draft_count
                FROM artifact_prompt_annotations
                WHERE session_key = ? AND session_id = ? AND session_epoch = ?
                  AND status = ?
                """,
                (
                    session_key,
                    session_id,
                    session_epoch,
                    PromptAnnotationStatus.DRAFT.value,
                ),
            )
            if count_row is not None and int(count_row["draft_count"]) >= 16:
                raise ArtifactValidationError("a session may contain at most 16 draft annotations")

            anchor_id = self._id_factory("anchor")
            await conn.execute(
                """
                INSERT INTO artifact_anchors (
                    anchor_id, document_id, revision_id, kind, locator_json,
                    quote, context_json, state, remapped_from_anchor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    anchor_id,
                    document_id,
                    revision_id,
                    kind.value,
                    _json_dumps(locator),
                    quote,
                    None if context is None else _json_dumps(context),
                    AnchorState.RESOLVED.value,
                    now,
                ),
            )
            await self._append_audit(
                conn,
                document_id=document_id,
                event_type="anchor.created",
                actor=actor,
                revision_id=revision_id,
                anchor_id=anchor_id,
                payload={"kind": kind.value, "remapped_from_anchor_id": None},
                created_at=now,
            )
            await self._insert_prompt_annotation_on_conn(
                conn,
                annotation_id=annotation_id,
                session_key=session_key,
                session_id=session_id,
                session_epoch=session_epoch,
                document_id=document_id,
                revision_id=revision_id,
                anchor_id=anchor_id,
                body=body,
                now=now,
            )
            return (
                await self._get_anchor_on_conn(conn, anchor_id),
                await self._get_prompt_annotation_on_conn(conn, annotation_id),
            )

    async def create_prompt_annotation(
        self,
        *,
        annotation_id: str,
        session_key: str,
        session_id: str,
        session_epoch: int,
        document_id: str,
        revision_id: str,
        anchor_id: str,
        body: str,
    ) -> PromptAnnotation:
        """Create an idempotent draft bound to the exact current head and anchor."""

        now = self._clock()
        async with self._transaction("create_prompt_annotation") as conn:
            existing_row = await _fetchone(
                conn,
                "SELECT * FROM artifact_prompt_annotations WHERE annotation_id = ?",
                (annotation_id,),
            )
            if existing_row is not None:
                existing = _prompt_annotation_from_row(existing_row)
                expected = (
                    session_key,
                    session_id,
                    session_epoch,
                    document_id,
                    revision_id,
                    anchor_id,
                    body,
                )
                actual = (
                    existing.session_key,
                    existing.session_id,
                    existing.session_epoch,
                    existing.document_id,
                    existing.revision_id,
                    existing.anchor_id,
                    existing.body,
                )
                if existing.status is PromptAnnotationStatus.DRAFT and actual == expected:
                    return existing
                raise ArtifactConflictError("prompt annotation id is already in use")

            document = await self._get_document_on_conn(conn, document_id)
            if document.session_key != session_key or document.session_id != session_id:
                raise ArtifactNotFoundError(f"document not found: {document_id}")
            if document.head_revision_id != revision_id:
                raise ArtifactConflictError("prompt annotation revision is no longer current")
            revision = await self._get_revision_on_conn(conn, revision_id)
            if revision.document_id != document_id:
                raise ArtifactValidationError(
                    "prompt annotation revision belongs to another document"
                )
            anchor = await self._get_anchor_on_conn(conn, anchor_id)
            if (
                anchor.document_id != document_id
                or anchor.revision_id != revision_id
                or anchor.state is not AnchorState.RESOLVED
            ):
                raise ArtifactValidationError(
                    "prompt annotation anchor does not match its revision"
                )
            count_row = await _fetchone(
                conn,
                """
                SELECT COUNT(*) AS draft_count
                FROM artifact_prompt_annotations
                WHERE session_key = ? AND session_id = ? AND session_epoch = ?
                  AND status = ?
                """,
                (
                    session_key,
                    session_id,
                    session_epoch,
                    PromptAnnotationStatus.DRAFT.value,
                ),
            )
            if count_row is not None and int(count_row["draft_count"]) >= 16:
                raise ArtifactValidationError("a session may contain at most 16 draft annotations")
            await self._insert_prompt_annotation_on_conn(
                conn,
                annotation_id=annotation_id,
                session_key=session_key,
                session_id=session_id,
                session_epoch=session_epoch,
                document_id=document_id,
                revision_id=revision_id,
                anchor_id=anchor_id,
                body=body,
                now=now,
            )
            return await self._get_prompt_annotation_on_conn(conn, annotation_id)

    async def get_prompt_annotation(self, annotation_id: str) -> PromptAnnotation:
        async with self._transaction("get_prompt_annotation") as conn:
            return await self._get_prompt_annotation_on_conn(conn, annotation_id)

    async def list_prompt_annotations(
        self,
        *,
        session_key: str,
        session_id: str,
        session_epoch: int,
        status: PromptAnnotationStatus | None = None,
        document_id: str | None = None,
        limit: int = 500,
    ) -> tuple[PromptAnnotation, ...]:
        async with self._transaction("list_prompt_annotations") as conn:
            conditions = ["session_key = ?", "session_id = ?", "session_epoch = ?"]
            values: list[Any] = [session_key, session_id, session_epoch]
            if status is not None:
                conditions.append("status = ?")
                values.append(status.value)
            if document_id is not None:
                conditions.append("document_id = ?")
                values.append(document_id)
            values.append(limit)
            rows = await _fetchall(
                conn,
                f"""
                SELECT * FROM artifact_prompt_annotations
                WHERE {" AND ".join(conditions)}
                ORDER BY created_at, annotation_id
                LIMIT ?
                """,  # noqa: S608 - conditions are fixed server-side fragments.
                values,
            )
            return tuple(_prompt_annotation_from_row(row) for row in rows)

    async def update_prompt_annotation(
        self,
        *,
        annotation_id: str,
        expected_state_revision: int,
        body: str,
    ) -> PromptAnnotation:
        now = self._clock()
        async with self._transaction("update_prompt_annotation") as conn:
            annotation = await self._get_prompt_annotation_on_conn(conn, annotation_id)
            if annotation.status is not PromptAnnotationStatus.DRAFT:
                raise ArtifactConflictError("only draft prompt annotations may be updated")
            if annotation.state_revision != expected_state_revision:
                raise ArtifactConflictError("prompt annotation state_revision changed")
            cursor = await conn.execute(
                """
                UPDATE artifact_prompt_annotations
                SET body = ?, state_revision = state_revision + 1, updated_at = ?
                WHERE annotation_id = ? AND state_revision = ? AND status = ?
                """,
                (
                    body,
                    now,
                    annotation_id,
                    expected_state_revision,
                    PromptAnnotationStatus.DRAFT.value,
                ),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError("prompt annotation compare-and-swap failed")
            finally:
                await cursor.close()
            return await self._get_prompt_annotation_on_conn(conn, annotation_id)

    async def discard_prompt_annotation(
        self,
        *,
        annotation_id: str,
        expected_state_revision: int,
    ) -> PromptAnnotation:
        now = self._clock()
        async with self._transaction("discard_prompt_annotation") as conn:
            annotation = await self._get_prompt_annotation_on_conn(conn, annotation_id)
            if annotation.status is not PromptAnnotationStatus.DRAFT:
                raise ArtifactConflictError("only draft prompt annotations may be discarded")
            if annotation.state_revision != expected_state_revision:
                raise ArtifactConflictError("prompt annotation state_revision changed")
            cursor = await conn.execute(
                """
                UPDATE artifact_prompt_annotations
                SET status = ?, state_revision = state_revision + 1, updated_at = ?
                WHERE annotation_id = ? AND state_revision = ? AND status = ?
                """,
                (
                    PromptAnnotationStatus.DISCARDED.value,
                    now,
                    annotation_id,
                    expected_state_revision,
                    PromptAnnotationStatus.DRAFT.value,
                ),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError("prompt annotation compare-and-swap failed")
            finally:
                await cursor.close()
            return await self._get_prompt_annotation_on_conn(conn, annotation_id)

    async def preflight_prompt_annotations(
        self,
        *,
        annotation_ids: Sequence[str],
        session_key: str,
        session_id: str,
        session_epoch: int,
        require_current_head: bool = True,
    ) -> tuple[PromptAnnotation, ...]:
        async with self._transaction("preflight_prompt_annotations") as conn:
            return await preflight_prompt_annotations_on_conn(
                conn,
                annotation_ids=annotation_ids,
                session_key=session_key,
                session_id=session_id,
                session_epoch=session_epoch,
                require_current_head=require_current_head,
            )

    async def start_edit_session(
        self,
        *,
        document_id: str,
        user_id: str,
        ttl_ms: int,
        actor: Actor,
        edit_session_id: str,
    ) -> EditSession:
        """Open an editor lifecycle session without acquiring write authority.

        A caller-derived opaque ``edit_session_id`` is an idempotency key. An
        exact replay returns the original live session; reusing it for another
        request fails closed. Writer leases are acquired only for an individual
        save and are never retained by the editor heartbeat.
        """

        now = self._clock()
        async with self._transaction("start_edit_session") as conn:
            document = await self._get_document_on_conn(conn, document_id)
            existing_row = await _fetchone(
                conn,
                "SELECT * FROM artifact_edit_sessions WHERE edit_session_id = ?",
                (edit_session_id,),
            )
            if existing_row is not None:
                existing = _edit_session_from_row(existing_row)
                if (
                    existing.document_id != document_id
                    or existing.user_id != user_id
                    or existing.mode is not EditSessionMode.EDIT
                ):
                    raise ArtifactConflictError(
                        "edit session request was already used for another editor"
                    )
                if existing.status is not EditSessionStatus.ACTIVE:
                    raise ArtifactConflictError("edit session is not active")
                if existing.expires_at <= now:
                    raise ArtifactConflictError("edit session has expired")
                return existing

            await conn.execute(
                """
                INSERT INTO artifact_edit_sessions (
                    edit_session_id, document_id, base_revision_id,
                    last_saved_revision_id, mode, status, user_id,
                    state_revision,
                    expires_at, last_access_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    edit_session_id,
                    document_id,
                    document.head_revision_id,
                    document.head_revision_id,
                    EditSessionMode.EDIT.value,
                    EditSessionStatus.ACTIVE.value,
                    user_id,
                    now + ttl_ms,
                    now,
                    now,
                    now,
                ),
            )
            await self._append_audit(
                conn,
                document_id=document_id,
                event_type="edit_session.opened",
                actor=actor,
                revision_id=document.head_revision_id,
                edit_session_id=edit_session_id,
                payload={"mode": EditSessionMode.EDIT.value, "expires_at": now + ttl_ms},
                created_at=now,
            )
            return await self._get_edit_session_on_conn(conn, edit_session_id)

    async def get_edit_session(self, edit_session_id: str) -> EditSession:
        async with self._transaction("get_edit_session") as conn:
            return await self._get_edit_session_on_conn(conn, edit_session_id)

    async def validate_edit_session_for_save(
        self,
        *,
        edit_session_id: str,
        document_id: str,
        user_id: str,
        expected_state_revision: int,
        expected_last_saved_revision_id: str,
    ) -> EditSession:
        """Resolve a live scoped edit session before acquiring a short save lease."""

        now = self._clock()
        async with self._transaction("validate_edit_session_for_save") as conn:
            edit_session = await self._get_edit_session_on_conn(conn, edit_session_id)
            if edit_session.document_id != document_id:
                raise ArtifactConflictError("edit session belongs to another document")
            if edit_session.user_id != user_id:
                raise ArtifactConflictError("edit session belongs to another user")
            if edit_session.mode is not EditSessionMode.EDIT:
                raise ArtifactConflictError("edit session is read-only")
            if edit_session.status is not EditSessionStatus.ACTIVE:
                raise ArtifactConflictError("edit session is not active")
            if edit_session.expires_at <= now:
                raise ArtifactConflictError("edit session has expired")
            if (
                edit_session.state_revision != expected_state_revision
                or edit_session.last_saved_revision_id != expected_last_saved_revision_id
            ):
                raise ArtifactConflictError("edit session save position changed")
            return edit_session

    async def heartbeat_edit_session(
        self,
        *,
        edit_session_id: str,
        user_id: str,
        expected_state_revision: int,
        ttl_ms: int,
        actor: Actor,
    ) -> EditSession:
        """Touch the editor lifecycle without acquiring or renewing a writer lease."""

        now = self._clock()
        expires_at = now + ttl_ms
        async with self._transaction("heartbeat_edit_session") as conn:
            edit_session = await self._get_edit_session_on_conn(conn, edit_session_id)
            if edit_session.user_id != user_id:
                raise ArtifactConflictError("edit session belongs to another user")
            if edit_session.state_revision != expected_state_revision:
                raise ArtifactConflictError("edit session state_revision changed")
            if edit_session.mode is not EditSessionMode.EDIT:
                raise ArtifactConflictError("edit session is read-only")
            if edit_session.status is not EditSessionStatus.ACTIVE:
                raise ArtifactConflictError("edit session is not active")
            if edit_session.expires_at <= now:
                raise ArtifactConflictError("edit session has expired")
            cursor = await conn.execute(
                """
                UPDATE artifact_edit_sessions
                SET state_revision = state_revision + 1, expires_at = ?,
                    last_access_at = ?, updated_at = ?
                WHERE edit_session_id = ? AND state_revision = ? AND status = ?
                """,
                (
                    expires_at,
                    now,
                    now,
                    edit_session_id,
                    expected_state_revision,
                    EditSessionStatus.ACTIVE.value,
                ),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError("edit session compare-and-swap failed")
            finally:
                await cursor.close()
            await self._append_audit(
                conn,
                document_id=edit_session.document_id,
                event_type="edit_session.touched",
                actor=actor,
                edit_session_id=edit_session_id,
                payload={"expires_at": expires_at},
                created_at=now,
            )
            return await self._get_edit_session_on_conn(conn, edit_session_id)

    async def close_edit_session(
        self,
        *,
        edit_session_id: str,
        user_id: str,
        expected_state_revision: int,
        actor: Actor,
    ) -> EditSession:
        """Close a lifecycle-only edit session without touching writer leases."""

        now = self._clock()
        async with self._transaction("close_edit_session") as conn:
            edit_session = await self._get_edit_session_on_conn(conn, edit_session_id)
            if edit_session.user_id != user_id:
                raise ArtifactConflictError("edit session belongs to another user")
            # CLOSED is terminal, so a response-loss retry can safely return it
            # even though the request carries the pre-close state revision.
            if edit_session.status is EditSessionStatus.CLOSED:
                return edit_session
            if edit_session.state_revision != expected_state_revision:
                raise ArtifactConflictError("edit session state_revision changed")
            if edit_session.status is not EditSessionStatus.ACTIVE:
                raise ArtifactConflictError("edit session is not active")

            cursor = await conn.execute(
                """
                UPDATE artifact_edit_sessions
                SET status = ?, state_revision = state_revision + 1,
                    last_access_at = ?, updated_at = ?
                WHERE edit_session_id = ? AND state_revision = ? AND status = ?
                """,
                (
                    EditSessionStatus.CLOSED.value,
                    now,
                    now,
                    edit_session_id,
                    expected_state_revision,
                    EditSessionStatus.ACTIVE.value,
                ),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError("edit session compare-and-swap failed")
            finally:
                await cursor.close()

            await self._append_audit(
                conn,
                document_id=edit_session.document_id,
                event_type="edit_session.closed",
                actor=actor,
                revision_id=edit_session.last_saved_revision_id,
                edit_session_id=edit_session_id,
                created_at=now,
            )
            return await self._get_edit_session_on_conn(conn, edit_session_id)

    async def list_audit_events(
        self,
        document_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> tuple[AuditEvent, ...]:
        """Read append-only audit events in deterministic sequence order."""

        async with self._transaction("list_audit_events") as conn:
            await self._get_document_on_conn(conn, document_id)
            rows = await _fetchall(
                conn,
                """
                SELECT * FROM artifact_audit_events
                WHERE document_id = ? AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (document_id, after_sequence, limit),
            )
            return tuple(_audit_event_from_row(row) for row in rows)

    async def latest_audit_event(self, document_id: str) -> AuditEvent | None:
        """Return the newest durable event for monotonic client invalidation."""

        async with self._transaction("latest_audit_event") as conn:
            await self._get_document_on_conn(conn, document_id)
            row = await _fetchone(
                conn,
                """
                SELECT * FROM artifact_audit_events
                WHERE document_id = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (document_id,),
            )
            return None if row is None else _audit_event_from_row(row)
