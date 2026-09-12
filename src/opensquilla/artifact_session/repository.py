"""Transactional SQLite repository for durable ArtifactSession state."""

from __future__ import annotations

import asyncio
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
    PromptAnnotation,
    PromptAnnotationStatus,
    Revision,
    RevisionSource,
    WriterLease,
    head_restore_receipt_state_revision,
)
from .retirement import RETIREMENT_STATEMENTS
from .schema import SCHEMA_STATEMENTS

TransactionFactory = Callable[[str], AbstractAsyncContextManager[Any]]
Clock = Callable[[], int]
IdFactory = Callable[[str], str]

_MUTATION_ATTEMPT_TURN_QUERY_CHUNK_SIZE = 400
# Audit rows that identify a durable revision-producing mutation.  Metadata
# events such as ``document.renamed`` can carry the current head revision id,
# but must not be mistaken for the commit that produced that revision when a
# source.patched notification is replayed.
_DURABLE_MUTATION_AUDIT_EVENT_TYPES = (
    "document.created",
    "document.restored",
    "document.reverted",
    "revision.committed",
    "revision.change_set_applied",
    "change_set.applied",
)


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
            await conn.execute("""
                INSERT OR IGNORE INTO artifact_working_source_versions
                SELECT working.base_revision_id, working.document_id,
                       working.relative_root, working.entrypoint,
                       source.bundle_mode, source.bundle_root
                FROM artifact_working_files AS working
                JOIN artifact_working_sources AS source USING(document_id)
            """)

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
        working_source: dict[str, str] | None = None,
    ) -> tuple[CommitResult, DocumentSourceBinding, bool]:
        """Atomically adopt and bind one public generated deliverable.

        ``created`` reports whether this call created the source binding.  A
        legacy Document that already references the same immutable artifact is
        reused and bound instead of being duplicated.
        """

        async with self._transaction("adopt_generated_deliverable") as conn:
            if working_source is not None:
                source_row = await _fetchone(conn, """
                    SELECT document_id FROM artifact_working_sources
                    WHERE session_key=? AND session_id=? AND workspace=? AND source_path=?
                """, (session_key, session_id, working_source["workspace"],
                       working_source["source_path"]))
                if source_row is not None:
                    document = await self._get_document_on_conn(conn, source_row["document_id"])
                    revision = await self._get_revision_on_conn(conn, document.head_revision_id)
                    bound = await _fetchone(conn,
                        "SELECT * FROM document_source_bindings WHERE document_id=?",
                        (document.document_id,))
                    if bound is None:
                        raise ArtifactConflictError("Working source has no document origin")
                    return CommitResult(document=document, revision=revision), (
                        _document_source_binding_from_row(bound)
                    ), False
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
            if working_source is not None:
                await conn.execute(
                    "INSERT INTO artifact_working_files VALUES (?, ?, ?, ?, ?)",
                    (commit.document.document_id, working_source["workspace"],
                     working_source["relative_root"], working_source["entrypoint"],
                     commit.revision.revision_id),
                )
                await conn.execute("""
                    INSERT INTO artifact_working_sources (
                        document_id, session_key, session_id, workspace, source_path,
                        bundle_mode, bundle_root
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (commit.document.document_id, session_key, session_id,
                       working_source["workspace"], working_source["source_path"],
                       working_source["bundle_mode"], working_source["bundle_root"] or None))
                await conn.execute("""
                    INSERT INTO artifact_working_source_versions VALUES (?, ?, ?, ?, ?, ?)
                """, (commit.revision.revision_id, commit.document.document_id,
                       working_source["relative_root"], working_source["entrypoint"],
                       working_source["bundle_mode"], working_source["bundle_root"] or None))
            return commit, binding, True

    async def retire_legacy_html_state(self) -> None:
        """Apply the same idempotent retirement as migration V041 in one transaction."""
        async with self._transaction("retire_legacy_html_state") as conn:
            for statement in RETIREMENT_STATEMENTS:
                await conn.execute(statement)

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
            payload={
                "generation": generation,
                "parent_revision_id": document.head_revision_id,
                "copied_from_revision_id": copied_from_revision_id,
                "source": source.value,
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
        turn_id: str | None = None,
        no_op: bool | None = None,
    ) -> CommitResult:
        """Select an existing revision without allocating another content version."""

        async with self._transaction("restore_revision") as conn:
            result, _, _ = await self._restore_revision_on_conn(
                conn,
                document_id=document_id,
                target_revision_id=target_revision_id,
                expected_head_revision_id=expected_head_revision_id,
                expected_state_revision=expected_state_revision,
                actor=actor,
                turn_id=turn_id,
                no_op=no_op,
            )
            return result

    async def _restore_revision_on_conn(
        self,
        conn: Any,
        *,
        document_id: str,
        target_revision_id: str,
        expected_head_revision_id: str,
        expected_state_revision: int,
        actor: Actor,
        turn_id: str | None = None,
        no_op: bool | None = None,
    ) -> tuple[CommitResult, ChangeSet | None, bool]:
        """Restore head within the caller's working-file transaction.

        A replay returns the original result and the current document without
        mutating either. Callers must skip filesystem restoration on replay.
        Pass no_op=False when the head is already selected but working files
        need restoration; the state epoch must fence that real change too.
        """

        if no_op is not None and type(no_op) is not bool:
            raise ArtifactValidationError("no_op must be a boolean")
        document = await self._get_document_on_conn(conn, document_id)
        target = await self._get_revision_on_conn(conn, target_revision_id)
        if target.document_id != document_id:
            raise ArtifactValidationError("target revision belongs to another document")
        operations = ({
            "op": "restore_revision",
            "target_revision_id": target_revision_id,
            "target_sha256": target.artifact_sha256,
            "expected_document_state_revision": expected_state_revision,
        },)
        change: ChangeSet | None = None
        if turn_id is not None:
            row = await _fetchone(
                conn, "SELECT * FROM artifact_change_sets WHERE turn_id = ?", (turn_id,),
            )
            if row is not None:
                change = _change_set_from_row(row)
                if (
                    change.document_id != document_id
                    or change.base_revision_id != expected_head_revision_id
                    or change.operations != operations
                    or change.candidate_artifact_id != target.artifact_id
                    or change.candidate_artifact_sha256 != target.artifact_sha256
                ):
                    raise ArtifactConflictError("request was used for a different document restore")
                if change.status is not ChangeSetStatus.APPLIED or not change.applied_revision_id:
                    raise ArtifactConflictError("document restoration receipt is not applied")
                applied = await self._get_revision_on_conn(conn, change.applied_revision_id)
                state = head_restore_receipt_state_revision(change, applied)
                if state is None and (
                    applied.document_id != document_id
                    or applied.change_set_id != change.change_set_id
                    or applied.artifact != target.artifact
                    or applied.source is not RevisionSource.RESTORE
                    or applied.copied_from_revision_id != target_revision_id
                ):
                    raise ArtifactConflictError("document restoration receipt is inconsistent")
                return CommitResult(document=document, revision=applied), change, True
        if (
            document.head_revision_id != expected_head_revision_id
            or document.state_revision != expected_state_revision
        ):
            raise ArtifactConflictError("document head changed; refresh head and state revision")
        same_head = document.head_revision_id == target_revision_id
        if no_op is True and not same_head:
            raise ArtifactValidationError("a head change cannot be a no-op")
        unchanged = same_head if no_op is None else no_op
        result_state = document.state_revision + (0 if unchanged else 1)
        now = self._clock()
        if not unchanged:
            cursor = await conn.execute(
                "UPDATE artifact_documents SET head_revision_id=?, state_revision=?, updated_at=? "
                "WHERE document_id=? AND head_revision_id=? AND state_revision=?",
                (target_revision_id, result_state, now, document_id,
                 expected_head_revision_id, expected_state_revision),
            )
            try:
                if cursor.rowcount != 1:
                    raise ArtifactConflictError("document head compare-and-swap failed")
            finally:
                await cursor.close()
        change = None
        if turn_id is not None:
            change_id = self._id_factory("change")
            await conn.execute(
                """
                INSERT INTO artifact_change_sets (
                    change_set_id, document_id, base_revision_id, turn_id,
                    summary, status, operations_json, candidate_artifact_id,
                    candidate_artifact_sha256, candidate_filename, candidate_media_type,
                    candidate_byte_size, validation_json, state_revision,
                    created_by_kind, created_by_id, applied_revision_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 3, ?, ?, ?, ?, ?)
                """,
                (change_id, document_id, expected_head_revision_id, turn_id,
                 "Restore document revision", ChangeSetStatus.APPLIED.value,
                 _json_dumps(list(operations)), target.artifact_id, target.artifact_sha256,
                 target.filename, target.media_type, target.byte_size,
                 _json_dumps({"restore_mode": "head_pointer",
                              "result_state_revision": result_state, "no_op": unchanged}),
                 actor.kind.value, actor.actor_id, target_revision_id, now, now),
            )
            change = await self._get_change_set_on_conn(conn, change_id)
        if not unchanged:
            await self._append_audit(
                conn,
                document_id=document_id,
                event_type="document.restored",
                actor=actor,
                revision_id=target_revision_id,
                change_set_id=change.change_set_id if change else None,
                payload={"previous_head_revision_id": expected_head_revision_id,
                         "target_revision_id": target_revision_id,
                         "result_state_revision": result_state},
                created_at=now,
            )
        updated = await self._get_document_on_conn(conn, document_id)
        return CommitResult(document=updated, revision=target), change, False

    async def revert_revision(
        self,
        *,
        document_id: str,
        target_revision_id: str,
        expected_head_revision_id: str,
        expected_state_revision: int,
        actor: Actor,
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
        )

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
                      AND NOT (
                        COALESCE(json_extract(validation_json, '$.restore_mode'), '')
                            = 'head_pointer'
                        AND COALESCE(json_type(validation_json, '$.no_op'), '') = 'true'
                      )
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
                      AND NOT (
                        COALESCE(json_extract(validation_json, '$.restore_mode'), '')
                            = 'head_pointer'
                        AND COALESCE(json_type(validation_json, '$.no_op'), '') = 'true'
                      )
                    ORDER BY updated_at DESC, change_set_id
                    LIMIT ?
                    """,
                    (document_id, status.value, limit),
                )
            return tuple(_change_set_from_row(row) for row in rows)

    async def list_draft_change_sets(self, *, limit: int = 100) -> tuple[ChangeSet, ...]:
        async with self._read_transaction("list_draft_change_sets") as conn:
            rows = await _fetchall(
                conn,
                """SELECT * FROM artifact_change_sets WHERE status = 'draft'
                   ORDER BY updated_at ASC, change_set_id LIMIT ?""",
                (limit,),
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
            return result, await self._get_change_set_on_conn(conn, change_set_id)

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

    async def get_mutation_attempt_for_resolution(
        self,
        *,
        document_id: str,
        turn_id: str,
    ) -> MutationAttempt:
        """Load a receipt for a session-scoped product outcome query.

        The authenticated resolution RPC checks the owning document before
        reading a historical receipt. This method never resumes execution.
        """

        async with self._read_transaction("get_mutation_attempt_for_resolution") as conn:
            return await self._get_mutation_attempt_on_conn(
                conn,
                document_id=document_id,
                turn_id=turn_id,
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

    async def get_edit_session(self, edit_session_id: str) -> EditSession:
        async with self._transaction("get_edit_session") as conn:
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

    async def audit_event_for_mutation(
        self,
        document_id: str,
        *,
        revision_id: str | None = None,
        change_set_id: str | None = None,
    ) -> AuditEvent | None:
        """Return the newest audit row for one exact durable mutation.

        Runtime state notifications are delivered out of band, after the
        revision transaction commits.  Recovery must therefore derive the
        event sequence from the mutation that was actually committed rather
        than from whichever unrelated audit row happens to be newest.  At
        least one immutable mutation identifier is required; when both are
        supplied the match is conjunctive.
        """

        if revision_id is None and change_set_id is None:
            raise ArtifactValidationError(
                "revision_id or change_set_id is required for an exact audit lookup"
            )
        clauses = ["document_id = ?"]
        params: list[Any] = [document_id]
        if revision_id is not None:
            clauses.append("revision_id = ?")
            params.append(revision_id)
        if change_set_id is not None:
            clauses.append("change_set_id = ?")
            params.append(change_set_id)
        # When both immutable identifiers are present they identify the
        # revision-producing audit row even for a caller-supplied custom
        # revision event type.  A revision-only lookup needs the event-type
        # fence because metadata events (rename/publish) may repeat the head
        # revision id.
        if revision_id is None or change_set_id is None:
            event_placeholders = ", ".join("?" for _ in _DURABLE_MUTATION_AUDIT_EVENT_TYPES)
            clauses.append(
                f"(event_type IN ({event_placeholders}) OR event_type LIKE 'revision.%')"
            )
            params.extend(_DURABLE_MUTATION_AUDIT_EVENT_TYPES)
        async with self._read_transaction("audit_event_for_mutation") as conn:
            await self._get_document_on_conn(conn, document_id)
            row = await _fetchone(
                conn,
                """
                SELECT * FROM artifact_audit_events
                WHERE """
                + " AND ".join(clauses)
                + " ORDER BY sequence DESC LIMIT 1",
                tuple(params),
            )
            return None if row is None else _audit_event_from_row(row)
