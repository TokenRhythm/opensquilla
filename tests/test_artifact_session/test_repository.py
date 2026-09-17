"""Repository-level invariants for durable artifact revision state."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    AnchorKind,
    ArtifactBlobRef,
    ArtifactConflictError,
    ArtifactKind,
    ArtifactSessionService,
    ArtifactValidationError,
    RevisionSource,
)
from opensquilla.artifact_session.models import ChangeSetStatus, head_restore_receipt_state_revision
from opensquilla.artifact_session.repository import ArtifactSessionRepository
from opensquilla.session.storage import SessionStorage


class FakeClock:
    def __init__(self, value: int = 1_900_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        self.value += 1
        return self.value

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds


class PredictableIds:
    def __init__(self) -> None:
        self._next = 0

    def __call__(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}_{self._next}"


USER = Actor(ActorKind.USER, "user-1")
AGENT = Actor(ActorKind.AGENT, "agent-1")


def blob(label: str, *, filename: str = "report.docx") -> ArtifactBlobRef:
    digest = label.encode().hex() or "00"
    return ArtifactBlobRef(
        artifact_id=f"artifact-{label}",
        sha256=(digest * 64)[:64],
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        byte_size=len(label),
    )


async def open_service(db_path: Path, clock: FakeClock) -> ArtifactSessionService:
    return await ArtifactSessionService.open(
        db_path,
        clock=clock,
        id_factory=PredictableIds(),
    )


@pytest.mark.asyncio
async def test_session_storage_schema_reconciliation_is_single_flight_per_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    calls = 0
    original = ArtifactSessionRepository.initialize

    async def counted_initialize(repository: ArtifactSessionRepository) -> None:
        nonlocal calls
        calls += 1
        await original(repository)

    monkeypatch.setattr(ArtifactSessionRepository, "initialize", counted_initialize)
    try:
        await asyncio.gather(
            *(ArtifactSessionService.from_session_storage(storage) for _ in range(8))
        )
        await ArtifactSessionService.from_session_storage(storage)
        assert calls == 1
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_revision_commit_uses_head_and_state_compare_and_swap(tmp_path: Path) -> None:
    service = await open_service(tmp_path / "artifacts.db", FakeClock())
    try:
        initial = await service.create_document(
            session_key="agent:main:webchat:one",
            name="Report",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=blob("one"),
            actor=USER,
        )

        committed = await service.commit_revision(
            document_id=initial.document.document_id,
            expected_head_revision_id=initial.revision.revision_id,
            expected_state_revision=initial.document.state_revision,
            artifact=blob("two"),
            actor=USER,
        )

        with pytest.raises(ArtifactConflictError, match="document head changed"):
            await service.commit_revision(
                document_id=initial.document.document_id,
                expected_head_revision_id=initial.revision.revision_id,
                expected_state_revision=initial.document.state_revision,
                artifact=blob("stale"),
                actor=USER,
            )

        assert committed.document.generation == 2
        assert committed.document.state_revision == 2
        assert committed.revision.parent_revision_id == initial.revision.revision_id
        assert [
            revision.generation
            for revision in await service.list_revisions(initial.document.document_id)
        ] == [2, 1]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_audit_event_for_mutation_ignores_later_unrelated_events(
    tmp_path: Path,
) -> None:
    """Replay uses the committed revision sequence, not document latest."""

    service = await open_service(tmp_path / "audit-replay.db", FakeClock())
    try:
        initial = await service.create_document(
            session_key="agent:main:webchat:audit-replay",
            name="Replay",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=blob("audit-base"),
            actor=USER,
        )
        committed = await service.commit_revision(
            document_id=initial.document.document_id,
            expected_head_revision_id=initial.revision.revision_id,
            expected_state_revision=initial.document.state_revision,
            artifact=blob("audit-next"),
            actor=AGENT,
            source=RevisionSource.AGENT,
        )
        await service.rename_document(
            document_id=initial.document.document_id,
            expected_state_revision=committed.document.state_revision,
            name="Replay renamed",
            actor=USER,
        )

        exact = await service.audit_event_for_mutation(
            initial.document.document_id,
            revision_id=committed.revision.revision_id,
        )
        assert exact is not None
        assert exact.revision_id == committed.revision.revision_id
        assert exact.event_type == "revision.committed"
        latest = await service.latest_audit_event(initial.document.document_id)
        assert latest is not None
        assert exact.sequence < latest.sequence

        with pytest.raises(ArtifactValidationError, match="revision_id or change_set_id"):
            await service.audit_event_for_mutation(initial.document.document_id)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_adopt_document_is_atomic_and_rejects_preexisting_ambiguity(
    tmp_path: Path,
) -> None:
    service = await open_service(tmp_path / "artifacts.db", FakeClock())
    try:
        artifact = blob("adopt-once")

        async def _adopt():
            return await service.adopt_document(
                session_key="agent:main:webchat:adopt",
                session_id="adopt-epoch",
                name="Adopt once",
                kind=ArtifactKind.DOCUMENT,
                initial_artifact=artifact,
                actor=USER,
            )

        results = await asyncio.gather(*(_adopt() for _ in range(12)))
        assert sum(adopted for _result, adopted in results) == 1
        assert len({result.document.document_id for result, _adopted in results}) == 1
        assert (
            len(
                await service.list_documents(
                    session_key="agent:main:webchat:adopt",
                    session_id="adopt-epoch",
                )
            )
            == 1
        )

        await service.create_document(
            session_key="agent:main:webchat:adopt",
            session_id="adopt-epoch",
            name="Ambiguous legacy duplicate",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=artifact,
            actor=USER,
        )
        with pytest.raises(ArtifactConflictError, match="multiple documents"):
            await _adopt()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_document_rename_is_state_cas_audited_and_invalidates_stale_head_writes(
    tmp_path: Path,
) -> None:
    service = await open_service(tmp_path / "artifacts.db", FakeClock())
    try:
        created = await service.create_document(
            session_key="agent:main:webchat:rename",
            name="Draft",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=blob("one"),
            actor=USER,
        )
        renamed = await service.rename_document(
            document_id=created.document.document_id,
            expected_state_revision=created.document.state_revision,
            name="Final report",
            actor=USER,
        )

        assert renamed.name == "Final report"
        assert renamed.head_revision_id == created.document.head_revision_id
        assert renamed.generation == created.document.generation
        assert renamed.state_revision == created.document.state_revision + 1

        with pytest.raises(ArtifactConflictError, match="state_revision changed"):
            await service.rename_document(
                document_id=created.document.document_id,
                expected_state_revision=created.document.state_revision,
                name="Stale rename",
                actor=USER,
            )
        with pytest.raises(ArtifactConflictError, match="document head changed"):
            await service.commit_revision(
                document_id=created.document.document_id,
                expected_head_revision_id=created.revision.revision_id,
                expected_state_revision=created.document.state_revision,
                artifact=blob("stale"),
                actor=USER,
            )

        events = await service.list_audit_events(created.document.document_id)
        assert [event.event_type for event in events] == [
            "document.created",
            "document.renamed",
        ]
        assert events[-1].payload == {"new_name": "Final report", "old_name": "Draft"}
        assert await service.latest_audit_event(created.document.document_id) == events[-1]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restore_moves_head_and_revert_preserves_append_only_content_history(
    tmp_path: Path,
) -> None:
    service = await open_service(tmp_path / "artifacts.db", FakeClock())
    try:
        initial = await service.create_document(
            session_key="agent:main:webchat:restore",
            name="Plan",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=blob("old"),
            actor=USER,
        )
        second = await service.commit_revision(
            document_id=initial.document.document_id,
            expected_head_revision_id=initial.revision.revision_id,
            expected_state_revision=initial.document.state_revision,
            artifact=blob("new"),
            actor=AGENT,
            source=RevisionSource.AGENT,
        )
        restored = await service.restore_revision(
            document_id=initial.document.document_id,
            target_revision_id=initial.revision.revision_id,
            expected_head_revision_id=second.revision.revision_id,
            expected_state_revision=second.document.state_revision,
            actor=USER,
        )
        reverted = await service.revert_revision(
            document_id=initial.document.document_id,
            target_revision_id=second.revision.revision_id,
            expected_head_revision_id=restored.revision.revision_id,
            expected_state_revision=restored.document.state_revision,
            actor=USER,
        )

        assert restored.revision == initial.revision
        assert restored.document.head_revision_id == initial.revision.revision_id
        assert restored.document.generation == second.document.generation
        assert restored.document.state_revision == second.document.state_revision + 1
        assert reverted.revision.source is RevisionSource.REVERT
        assert reverted.revision.parent_revision_id == restored.revision.revision_id
        assert reverted.revision.copied_from_revision_id == second.revision.revision_id
        assert [
            item.generation for item in await service.list_revisions(initial.document.document_id)
        ] == [3, 2, 1]

        events = await service.list_audit_events(initial.document.document_id)
        assert [event.event_type for event in events] == [
            "document.created",
            "revision.committed",
            "document.restored",
            "document.reverted",
        ]
        assert [event.sequence for event in events] == sorted(event.sequence for event in events)
    finally:
        await service.close()


@pytest.mark.parametrize("no_op", [None, True, False])
async def test_restore_current_head_keeps_content_and_records_exact_state(
    tmp_path: Path, no_op: bool | None,
) -> None:
    service = await open_service(tmp_path / "no-op.db", FakeClock())
    try:
        initial = await service.create_document(
            session_key="agent:main:webchat:no-op", name="Page",
            kind=ArtifactKind.HTML, initial_artifact=blob("initial"), actor=USER,
        )
        events = await service.list_audit_events(initial.document.document_id)
        restored = await service.restore_revision(
            document_id=initial.document.document_id,
            target_revision_id=initial.revision.revision_id,
            expected_head_revision_id=initial.revision.revision_id,
            expected_state_revision=initial.document.state_revision,
            actor=USER, turn_id="revision-restore:no-op", no_op=no_op,
        )
        receipt = await service.get_change_set_by_turn(
            document_id=initial.document.document_id, turn_id="revision-restore:no-op",
        )
        assert receipt is not None
        assert restored.revision == initial.revision
        assert restored.document.generation == 1
        assert await service.list_revisions(initial.document.document_id) == (initial.revision,)
        assert head_restore_receipt_state_revision(receipt, initial.revision) == (
            restored.document.state_revision
        )
        if no_op is False:
            # The working-file coordinator uses this when bytes, but not head, changed.
            assert restored.document.state_revision == initial.document.state_revision + 1
            assert len(await service.list_audit_events(initial.document.document_id)) == 2
        else:
            assert restored.document == initial.document
            assert await service.list_audit_events(initial.document.document_id) == events
        before_replay = await service.list_audit_events(initial.document.document_id)
        replayed = await service.restore_revision(
            document_id=initial.document.document_id,
            target_revision_id=initial.revision.revision_id,
            expected_head_revision_id=initial.revision.revision_id,
            expected_state_revision=initial.document.state_revision,
            actor=USER, turn_id="revision-restore:no-op",
        )
        assert replayed == restored
        assert await service.list_audit_events(initial.document.document_id) == before_replay
    finally:
        await service.close()


async def test_restore_receipt_replay_after_restart_preserves_later_edit_and_numbering(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "restore-replay.db"
    service = await open_service(db_path, FakeClock())
    try:
        initial = await service.create_document(
            session_key="agent:main:webchat:restore-replay", name="Page",
            kind=ArtifactKind.HTML, initial_artifact=blob("initial"), actor=USER,
        )
        second = await service.commit_revision(
            document_id=initial.document.document_id,
            expected_head_revision_id=initial.revision.revision_id,
            expected_state_revision=initial.document.state_revision,
            artifact=blob("second"), actor=AGENT,
        )
        command = dict(
            document_id=initial.document.document_id,
            target_revision_id=initial.revision.revision_id,
            expected_head_revision_id=second.revision.revision_id,
            expected_state_revision=second.document.state_revision,
            actor=USER, turn_id="revision-restore:once",
        )
        restored = await service.restore_revision(**command)
        with pytest.raises(ArtifactConflictError, match="document head changed"):
            await service.commit_revision(
                document_id=initial.document.document_id,
                expected_head_revision_id=initial.revision.revision_id,
                expected_state_revision=initial.document.state_revision,
                artifact=blob("stale"), actor=AGENT,
            )
        edited = await service.commit_revision(
            document_id=initial.document.document_id,
            expected_head_revision_id=restored.revision.revision_id,
            expected_state_revision=restored.document.state_revision,
            artifact=blob("edited"), actor=AGENT,
        )
        assert edited.revision.generation == 3
        assert edited.revision.parent_revision_id == initial.revision.revision_id
        revisions = await service.list_revisions(initial.document.document_id)
        assert [revision.generation for revision in revisions] == [3, 2, 1]
        events = await service.list_audit_events(initial.document.document_id)
        await service.close()
        service = await open_service(db_path, FakeClock())
        replayed = await service.restore_revision(**command)
        assert replayed.document == edited.document
        assert replayed.revision == initial.revision
        receipt = await service.get_change_set_by_turn(
            document_id=initial.document.document_id, turn_id="revision-restore:once",
        )
        assert receipt is not None
        assert head_restore_receipt_state_revision(receipt, replayed.revision) == (
            restored.document.state_revision
        )
        assert await service.list_revisions(initial.document.document_id) == revisions
        assert await service.list_audit_events(initial.document.document_id) == events
        for changed in (
            {"target_revision_id": second.revision.revision_id},
            {"expected_head_revision_id": initial.revision.revision_id},
            {"expected_state_revision": second.document.state_revision + 1},
        ):
            with pytest.raises(ArtifactConflictError, match="different document restore"):
                await service.restore_revision(**(command | changed))
    finally:
        await service.close()


async def test_restore_keeps_legacy_copy_revision_and_receipt_readable(tmp_path: Path) -> None:
    service = await open_service(tmp_path / "legacy-restore.db", FakeClock())
    try:
        initial = await service.create_document(
            session_key="agent:main:webchat:legacy-restore", name="Page",
            kind=ArtifactKind.HTML, initial_artifact=blob("initial"), actor=USER,
        )
        second = await service.commit_revision(
            document_id=initial.document.document_id,
            expected_head_revision_id=initial.revision.revision_id,
            expected_state_revision=initial.document.state_revision,
            artifact=blob("second"), actor=AGENT,
        )
        legacy, legacy_receipt = await service.commit_change_set_atomically(
            document_id=initial.document.document_id,
            base_revision_id=second.revision.revision_id,
            expected_document_state_revision=second.document.state_revision,
            operations=({"op": "restore_revision",
                         "target_revision_id": initial.revision.revision_id,
                         "target_sha256": initial.revision.artifact_sha256,
                         "expected_document_state_revision": second.document.state_revision},),
            candidate_artifact=initial.revision.artifact,
            validation={"status": "passed"}, actor=USER,
            turn_id="revision-restore:legacy", source=RevisionSource.RESTORE,
            copied_from_revision_id=initial.revision.revision_id,
            revision_event_type="document.restored",
        )
        selected = await service.restore_revision(
            document_id=initial.document.document_id,
            target_revision_id=second.revision.revision_id,
            expected_head_revision_id=legacy.revision.revision_id,
            expected_state_revision=legacy.document.state_revision,
            actor=USER,
        )
        replayed = await service.restore_revision(
            document_id=initial.document.document_id,
            target_revision_id=initial.revision.revision_id,
            expected_head_revision_id=second.revision.revision_id,
            expected_state_revision=second.document.state_revision,
            actor=USER, turn_id="revision-restore:legacy",
        )
        assert replayed.document == selected.document
        assert replayed.revision == legacy.revision
        assert await service.get_change_set(legacy_receipt.change_set_id) == legacy_receipt
        assert await service.list_revisions(initial.document.document_id) == (
            legacy.revision, second.revision, initial.revision,
        )
        assert head_restore_receipt_state_revision(legacy_receipt, legacy.revision) is None
    finally:
        await service.close()


async def test_restore_on_connection_rolls_back_head_receipt_and_audit_together(tmp_path: Path):
    service = await open_service(tmp_path / "atomic-restore.db", FakeClock())
    try:
        initial = await service.create_document(
            session_key="agent:main:webchat:atomic-restore", name="Page",
            kind=ArtifactKind.HTML, initial_artifact=blob("initial"), actor=USER,
        )
        second = await service.commit_revision(
            document_id=initial.document.document_id,
            expected_head_revision_id=initial.revision.revision_id,
            expected_state_revision=initial.document.state_revision,
            artifact=blob("second"), actor=AGENT,
        )
        events = await service.list_audit_events(initial.document.document_id)
        with pytest.raises(RuntimeError, match="binding persistence failed"):
            async with service.repository._transaction("restore-coordination") as conn:
                result, receipt, replayed = await service.repository._restore_revision_on_conn(
                    conn, document_id=initial.document.document_id,
                    target_revision_id=initial.revision.revision_id,
                    expected_head_revision_id=second.revision.revision_id,
                    expected_state_revision=second.document.state_revision,
                    actor=USER, turn_id="revision-restore:atomic",
                )
                assert result.revision == initial.revision
                assert receipt is not None and replayed is False
                raise RuntimeError("binding persistence failed")
        assert await service.get_document(initial.document.document_id) == second.document
        assert await service.list_audit_events(initial.document.document_id) == events
        assert await service.get_change_set_by_turn(
            document_id=initial.document.document_id, turn_id="revision-restore:atomic",
        ) is None
        assert await service.list_revisions(initial.document.document_id) == (
            second.revision, initial.revision,
        )
    finally:
        await service.close()


@pytest.mark.parametrize("invalid", ["target", "artifact", "state", "boolean-state", "no-op"])
async def test_head_restore_receipt_rejects_inconsistent_results(tmp_path: Path, invalid: str):
    service = await open_service(tmp_path / "receipt-validation.db", FakeClock())
    try:
        initial = await service.create_document(
            session_key="agent:main:webchat:receipt-validation", name="Page",
            kind=ArtifactKind.HTML, initial_artifact=blob("initial"), actor=USER,
        )
        await service.restore_revision(
            document_id=initial.document.document_id,
            target_revision_id=initial.revision.revision_id,
            expected_head_revision_id=initial.revision.revision_id,
            expected_state_revision=initial.document.state_revision,
            actor=USER, turn_id="revision-restore:validation",
        )
        receipt = await service.get_change_set_by_turn(
            document_id=initial.document.document_id, turn_id="revision-restore:validation",
        )
        assert receipt is not None and receipt.validation is not None
        if invalid == "target":
            receipt = replace(receipt, applied_revision_id="foreign-revision")
        elif invalid == "artifact":
            receipt = replace(receipt, candidate_artifact_id="foreign-artifact")
        else:
            changed = {"result_state_revision": True if invalid == "boolean-state" else 999}
            if invalid == "no-op":
                changed = {"no_op": "true"}
            receipt = replace(receipt, validation=receipt.validation | changed)
        with pytest.raises(ArtifactConflictError, match="receipt is inconsistent"):
            head_restore_receipt_state_revision(receipt, initial.revision)
    finally:
        await service.close()


@pytest.mark.parametrize("status", [None, ChangeSetStatus.APPLIED])
async def test_restore_no_op_receipts_do_not_consume_change_history_limit(tmp_path: Path, status):
    service = await open_service(tmp_path / "history-limit.db", FakeClock())
    try:
        initial = await service.create_document(
            session_key="agent:main:webchat:history-limit", name="Page",
            kind=ArtifactKind.HTML, initial_artifact=blob("initial"), actor=USER,
        )
        changed, visible_change = await service.commit_change_set_atomically(
            document_id=initial.document.document_id,
            base_revision_id=initial.revision.revision_id,
            expected_document_state_revision=initial.document.state_revision,
            operations=({"op": "update", "text": "Updated title"},),
            candidate_artifact=blob("updated"), validation=None,
            actor=AGENT, turn_id="edit:history-limit",
        )
        for index in range(3):
            await service.restore_revision(
                document_id=initial.document.document_id,
                target_revision_id=changed.revision.revision_id,
                expected_head_revision_id=changed.revision.revision_id,
                expected_state_revision=changed.document.state_revision,
                actor=USER, turn_id=f"revision-restore:no-op-{index}",
            )
        assert await service.list_change_sets(
            initial.document.document_id, status=status, limit=1,
        ) == (visible_change,)
        receipt = await service.get_change_set_by_turn(
            document_id=initial.document.document_id, turn_id="revision-restore:no-op-2",
        )
        assert receipt is not None
        assert await service.get_change_set(receipt.change_set_id) == receipt
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_session_fork_copies_only_current_heads_with_provenance(tmp_path: Path) -> None:
    db_path = tmp_path / "artifacts.db"
    service = await open_service(db_path, FakeClock())
    try:
        initial = await service.create_document(
            session_key="agent:main:webchat:parent",
            session_id="parent-epoch",
            name="Plan",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=blob("old"),
            actor=USER,
        )
        current = await service.commit_revision(
            document_id=initial.document.document_id,
            expected_head_revision_id=initial.revision.revision_id,
            expected_state_revision=initial.document.state_revision,
            artifact=blob("current"),
            actor=AGENT,
            source=RevisionSource.AGENT,
        )
        parent_anchor = await service.create_anchor(
            document_id=initial.document.document_id,
            revision_id=current.revision.revision_id,
            kind=AnchorKind.TEXT_RANGE,
            locator={"start": 0, "end": 1},
            actor=USER,
        )
        # A migrated profile may still contain historical editor rows.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """INSERT INTO artifact_prompt_annotations (
                    annotation_id, session_key, session_id, session_epoch, document_id,
                    revision_id, anchor_id, body, status, state_revision, created_at, updated_at
                ) VALUES ('parent-annotation', 'agent:main:webchat:parent', 'parent-epoch',
                          0, ?, ?, ?, 'Historical note', 'draft', 1, 1, 1)""",
                (
                    initial.document.document_id,
                    current.revision.revision_id,
                    parent_anchor.anchor_id,
                ),
            )
            conn.execute(
                """INSERT INTO artifact_edit_sessions (
                    edit_session_id, document_id, base_revision_id, last_saved_revision_id,
                    mode, status, user_id, state_revision, expires_at,
                    last_access_at, created_at, updated_at
                ) VALUES ('parent-editor', ?, ?, ?, 'edit', 'closed', 'user', 1, 0, 1, 1, 1)""",
                (
                    initial.document.document_id,
                    current.revision.revision_id,
                    current.revision.revision_id,
                ),
            )

        snapshots = await service.snapshot_session_heads(session_id="parent-epoch")
        forked = await service.fork_session_heads(
            source_session_id="parent-epoch",
            target_session_key="agent:main:webchat:child",
            target_session_id="child-epoch",
            snapshots=snapshots,
            actor=Actor(ActorKind.SYSTEM, "session-fork"),
        )

        assert len(forked) == 1
        child = forked[0]
        assert child.document.generation == 1
        assert child.document.state_revision == 1
        assert child.revision.parent_revision_id is None
        assert child.revision.copied_from_revision_id == current.revision.revision_id
        assert child.revision.artifact == current.revision.artifact
        assert [
            revision.generation
            for revision in await service.list_revisions(child.document.document_id)
        ] == [1]
        assert (
            await service.list_prompt_annotations(
                session_key="agent:main:webchat:child",
                session_id="child-epoch",
                session_epoch=0,
            )
            == ()
        )
        assert await service.list_documents(
            session_key="agent:main:webchat:parent",
            session_id="parent-epoch",
        ) == (current.document,)
        assert (
            await service.list_documents(
                session_key="agent:main:webchat:parent",
                session_id="different-epoch",
            )
            == ()
        )
        assert [
            event.event_type
            for event in await service.list_audit_events(child.document.document_id)
        ] == ["document.forked"]

        stale_snapshots = await service.snapshot_session_heads(session_id="parent-epoch")
        await service.rename_document(
            document_id=current.document.document_id,
            expected_state_revision=current.document.state_revision,
            name="Renamed while forking",
            actor=USER,
        )
        with pytest.raises(ArtifactConflictError, match="changed while session was forked"):
            await service.fork_session_heads(
                source_session_id="parent-epoch",
                target_session_key="agent:main:webchat:stale-child",
                target_session_id="stale-child-epoch",
                snapshots=stale_snapshots,
                actor=Actor(ActorKind.SYSTEM, "session-fork"),
            )
        assert (
            await service.list_documents(
                session_key="agent:main:webchat:stale-child",
                session_id="stale-child-epoch",
            )
            == ()
        )
    finally:
        await service.close()

    conn = sqlite3.connect(db_path)
    try:
        edit_session_count = conn.execute(
            "SELECT COUNT(*) FROM artifact_edit_sessions WHERE document_id = ?",
            (child.document.document_id,),
        ).fetchone()
        assert edit_session_count == (0,)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_runtime_schema_prevents_revision_and_audit_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "artifacts.db"
    service = await open_service(db_path, FakeClock())
    created = await service.create_document(
        session_key="agent:main:webchat:immutable",
        name="Immutable",
        kind=ArtifactKind.DOCUMENT,
        initial_artifact=blob("one"),
        actor=USER,
    )
    events = await service.list_audit_events(created.document.document_id)
    await service.close()

    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="revisions are immutable"):
            conn.execute(
                "UPDATE artifact_revisions SET filename = 'mutated' WHERE revision_id = ?",
                (created.revision.revision_id,),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="audit events are immutable"):
            conn.execute(
                "UPDATE artifact_audit_events SET event_type = 'mutated' WHERE sequence = ?",
                (events[0].sequence,),
            )
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_service_can_share_session_storage_connection_without_owning_it(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    service = await ArtifactSessionService.from_session_storage(
        storage,
        clock=FakeClock(),
        id_factory=PredictableIds(),
    )
    try:
        created = await service.create_document(
            session_key="agent:main:webchat:shared",
            name="Shared transaction gate",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=blob("one"),
            actor=USER,
        )
        assert (await service.get_document(created.document.document_id)).generation == 1

        await service.close()
        cursor = await storage.conn.execute("SELECT 1")
        try:
            row = await cursor.fetchone()
            assert row is not None and row[0] == 1
        finally:
            await cursor.close()
    finally:
        await service.close()
        await storage.close()


@pytest.mark.asyncio
async def test_cross_connection_writers_have_one_cas_winner(tmp_path: Path) -> None:
    db_path = tmp_path / "artifacts.db"
    first_service = await ArtifactSessionService.open(db_path)
    second_service = await ArtifactSessionService.open(db_path)
    try:
        created = await first_service.create_document(
            session_key="agent:main:webchat:race",
            name="CAS race",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=blob("one"),
            actor=USER,
        )

        async def commit(service: ArtifactSessionService, label: str) -> object:
            try:
                return await service.commit_revision(
                    document_id=created.document.document_id,
                    expected_head_revision_id=created.revision.revision_id,
                    expected_state_revision=created.document.state_revision,
                    artifact=blob(label),
                    actor=USER,
                )
            except ArtifactConflictError as exc:
                return exc

        results = await asyncio.gather(
            commit(first_service, "winner-a"),
            commit(second_service, "winner-b"),
        )

        assert sum(isinstance(item, ArtifactConflictError) for item in results) == 1
        revisions = await first_service.list_revisions(created.document.document_id)
        assert [revision.generation for revision in revisions] == [2, 1]
    finally:
        await first_service.close()
        await second_service.close()
