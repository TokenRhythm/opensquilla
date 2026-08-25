"""Storage contract tests for durable, idempotent turn acceptance."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    AnchorKind,
    ArtifactBlobRef,
    ArtifactConflictError,
    ArtifactKind,
    ArtifactSessionService,
    PromptAnnotationStatus,
)
from opensquilla.project_workspaces import (
    ProjectWorkspaceGuard,
    ProjectWorkspaceStateError,
)
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionNode,
    SessionStatus,
    TranscriptEntry,
)
from opensquilla.session.storage import (
    MetaLaunchDraftDiscardedError,
    SessionStorage,
    StorageBusyError,
    TurnIngressConflictError,
)

SESSION_KEY = "agent:main:webchat:durable-acceptance"
SESSION_ID = "session-durable-acceptance"


def _session(*, updated_at: int = 100) -> SessionNode:
    return SessionNode(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        agent_id="main",
        created_at=100,
        updated_at=updated_at,
        epoch=0,
    )


def _entry(message_id: str, *, content: str = "hello", created_at: int = 200) -> TranscriptEntry:
    return TranscriptEntry(
        session_id=SESSION_ID,
        session_key=SESSION_KEY,
        message_id=message_id,
        role="user",
        content=content,
        created_at=created_at,
    )


def _task(task_id: str, *, updated_at: int = 200) -> AgentTaskRecord:
    return AgentTaskRecord(
        task_id=task_id,
        session_key=SESSION_KEY,
        agent_id="main",
        source_kind="webui",
        queue_mode="followup",
        run_kind="web_turn",
        status=AgentTaskStatus.QUEUED,
        created_at=updated_at,
        updated_at=updated_at,
    )


async def _accept_turn(
    storage: SessionStorage,
    *,
    message_id: str,
    task_id: str,
    request_id: str = "request-one",
    fingerprint: str = "sha256:request-one",
    updated_at: int = 200,
) -> Any:
    return await storage.accept_turn(
        _entry(message_id, created_at=updated_at),
        expected_epoch=0,
        updated_at=updated_at,
        task_record=_task(task_id, updated_at=updated_at),
        source_scope="webui",
        request_session_key=SESSION_KEY,
        client_request_id=request_id,
        request_fingerprint=fingerprint,
    )


def _result_value(result: Any, name: str) -> Any:
    """Read an accepted identifier from either a result or its receipt member."""

    if isinstance(result, dict):
        candidate = result.get("receipt", result)
    else:
        candidate = getattr(result, "receipt", result)
    if isinstance(candidate, dict):
        return candidate[name]
    return getattr(candidate, name)


async def _row_count(storage: SessionStorage, table: str) -> int:
    async with storage.conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _receipt_rows(storage: SessionStorage) -> list[dict[str, Any]]:
    async with storage.conn.execute(
        "SELECT * FROM turn_ingress_receipts ORDER BY accepted_at, receipt_id"
    ) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def _prompt_annotation_draft(storage: SessionStorage):
    service = await ArtifactSessionService.from_session_storage(storage)
    created = await service.create_document(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        name="page.html",
        kind=ArtifactKind.HTML,
        initial_artifact=ArtifactBlobRef(
            artifact_id="artifact-html-1",
            sha256="a" * 64,
            filename="page.html",
            media_type="text/html",
            byte_size=13,
        ),
        actor=Actor(ActorKind.USER, "user-1"),
    )
    anchor = await service.create_anchor(
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        kind=AnchorKind.DOM_SOURCE,
        locator={"start_offset": 0, "start_tag_end_offset": 6, "tag_name": "main"},
        quote="<main>",
        actor=Actor(ActorKind.USER, "user-1"),
    )
    draft = await service.create_prompt_annotation(
        annotation_id="annotation-acceptance-1",
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        session_epoch=0,
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        anchor_id=anchor.anchor_id,
        body="Make this concise.",
    )
    return service, draft


@pytest.mark.asyncio
async def test_accept_turn_consumes_prompt_annotation_with_message_and_receipt(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        service, draft = await _prompt_annotation_draft(storage)

        accepted = await storage.accept_turn(
            _entry("annotation-message"),
            expected_epoch=0,
            updated_at=300,
            task_record=_task("annotation-turn", updated_at=300),
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id="annotation-request",
            request_fingerprint="sha256:annotation-request",
            expected_prompt_annotations=(draft,),
            prompt_annotation_turn_id="annotation-turn",
        )

        sent = await service.get_prompt_annotation(draft.annotation_id)
        assert accepted.replayed is False
        assert sent.status is PromptAnnotationStatus.SENT
        assert sent.sent_message_id == "annotation-message"
        assert sent.sent_turn_id == "annotation-turn"
        assert [item.message_id for item in await storage.get_transcript(SESSION_ID)] == [
            "annotation-message"
        ]

        replay = await storage.accept_turn(
            _entry("annotation-message"),
            expected_epoch=0,
            updated_at=301,
            task_record=_task("annotation-turn", updated_at=301),
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id="annotation-request",
            request_fingerprint="sha256:annotation-request",
            expected_prompt_annotations=(draft,),
            prompt_annotation_turn_id="annotation-turn",
        )
        assert replay.replayed is True
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_annotation_cas_failure_rolls_back_all_ingress_writes(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        service, draft = await _prompt_annotation_draft(storage)
        updated = await service.update_prompt_annotation(
            annotation_id=draft.annotation_id,
            expected_state_revision=draft.state_revision,
            body="Changed after preflight.",
        )

        with pytest.raises(ArtifactConflictError, match="changed after preflight"):
            await storage.accept_turn(
                _entry("rejected-annotation-message"),
                expected_epoch=0,
                updated_at=300,
                task_record=_task("rejected-annotation-turn", updated_at=300),
                source_scope="webui",
                request_session_key=SESSION_KEY,
                client_request_id="rejected-annotation-request",
                request_fingerprint="sha256:rejected-annotation-request",
                expected_prompt_annotations=(draft,),
                prompt_annotation_turn_id="rejected-annotation-turn",
            )

        assert await storage.get_transcript(SESSION_ID) == []
        assert await storage.get_agent_task("rejected-annotation-turn") is None
        assert await storage.get_turn_ingress_receipt(
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id="rejected-annotation-request",
        ) is None
        still_draft = await service.get_prompt_annotation(updated.annotation_id)
        assert still_draft.status is PromptAnnotationStatus.DRAFT
        assert still_draft.body == "Changed after preflight."
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_meta_control_staging_prunes_only_abandoned_rows_after_30_days(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        old, _ = await storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id="request:old-staged",
            meta_skill_name="meta-paper-write",
        )
        accepted, _ = await storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id="request:old-accepted",
            meta_skill_name="meta-paper-write",
        )
        await storage.conn.execute(
            "UPDATE meta_control_intents SET created_at = 1 WHERE intent_id IN (?, ?)",
            (old.intent_id, accepted.intent_id),
        )
        await storage.conn.execute(
            "UPDATE meta_control_intents SET status = 'accepted' WHERE intent_id = ?",
            (accepted.intent_id,),
        )
        await storage.conn.commit()

        recent, _ = await storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id="request:recent-staged",
            meta_skill_name="meta-paper-write",
        )

        assert await storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id="request:old-staged",
        ) is None
        assert await storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id="request:old-accepted",
        ) is not None
        assert await storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id="request:recent-staged",
        ) == recent
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_reset_epoch_invalidates_staged_and_fences_recent_accepted_controls(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        staged, _ = await storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id="request:staged-before-reset",
            meta_skill_name="meta-paper-write",
        )
        accepted, _ = await storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id="request:accepted-before-reset",
            meta_skill_name="meta-paper-write",
        )
        await storage.conn.execute(
            "UPDATE meta_control_intents SET status = 'accepted' WHERE intent_id = ?",
            (accepted.intent_id,),
        )
        await storage.conn.commit()

        assert await storage.advance_reset_epoch(SESSION_KEY) == 1

        assert await storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=staged.correlation_id,
        ) is None
        preserved = await storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=accepted.correlation_id,
        )
        assert preserved is not None
        assert preserved.intent_id == accepted.intent_id
        assert preserved.status == "accepted"
        assert await storage.is_meta_launch_discarded(
            session_key=SESSION_KEY,
            client_request_id="staged-before-reset",
        )
        assert await storage.is_meta_launch_discarded(
            session_key=SESSION_KEY,
            client_request_id="accepted-before-reset",
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_atomic_turn_reset_invalidates_staged_meta_controls(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    artifact_service: ArtifactSessionService | None = None
    try:
        await storage.upsert_session(_session())
        artifact_service = await ArtifactSessionService.from_session_storage(storage)
        await artifact_service.create_document(
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            name="old working draft",
            kind=ArtifactKind.HTML,
            initial_artifact=ArtifactBlobRef(
                artifact_id="art-old-working-draft",
                sha256="a" * 64,
                filename="draft.html",
                media_type="text/html",
                byte_size=10,
            ),
            actor=Actor(ActorKind.USER, "user-1"),
        )
        staged, _ = await storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id="request:before-atomic-reset",
            meta_skill_name="meta-paper-write",
        )
        await storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id="atomic-reset-turn",
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- stale collision with reset ingress",
        )
        reset_node = _session(updated_at=300)
        reset_node.session_id = "session-after-atomic-reset"
        reset_node.epoch = 1

        await storage.accept_turn(
            TranscriptEntry(
                session_id=reset_node.session_id,
                session_key=SESSION_KEY,
                message_id="message-after-atomic-reset",
                role="user",
                content="start over with this message",
                created_at=300,
            ),
            expected_epoch=1,
            updated_at=300,
            task_record=AgentTaskRecord(
                task_id="task-after-atomic-reset",
                session_key=SESSION_KEY,
                agent_id="main",
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.QUEUED,
                created_at=300,
                updated_at=300,
            ),
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id="atomic-reset-turn",
            request_fingerprint="sha256:atomic-reset-turn",
            session_node=reset_node,
            reset_from_session_id=SESSION_ID,
        )

        assert await storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=staged.correlation_id,
        ) is None
        rotated = await storage.get_session(SESSION_KEY)
        assert rotated is not None
        assert rotated.session_id == reset_node.session_id
        assert rotated.epoch == 1
        assert (
            await artifact_service.list_documents(
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
            )
            == ()
        )
        with pytest.raises(MetaLaunchDraftDiscardedError):
            await storage.stage_meta_launch_draft(
                session_key=SESSION_KEY,
                client_request_id="atomic-reset-turn",
                meta_skill_name="meta-paper-write",
                launch_text="/meta meta-paper-write -- stale collision with reset ingress",
            )
    finally:
        if artifact_service is not None:
            await artifact_service.close()
        await storage.close()


@pytest.mark.asyncio
async def test_atomic_turn_reset_preserves_only_control_accepted_by_new_turn(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        stale, _ = await storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id="request:stale-before-reset",
            meta_skill_name="meta-paper-write",
        )
        accepted_old, _ = await storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id="request:accepted-before-atomic-reset",
            meta_skill_name="meta-paper-write",
        )
        await storage.conn.execute(
            "UPDATE meta_control_intents SET status = 'accepted' WHERE intent_id = ?",
            (accepted_old.intent_id,),
        )
        await storage.conn.commit()
        current, _ = await storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id="request:current-reset-turn",
            meta_skill_name="meta-paper-write",
        )
        control = {
            "version": 1,
            "intent_id": current.intent_id,
            "kind": "manual",
            "name": "meta-paper-write",
            "correlation_id": current.correlation_id,
        }
        reset_node = _session(updated_at=300)
        reset_node.session_id = "session-after-meta-control-reset"
        reset_node.epoch = 1
        entry = TranscriptEntry(
            session_id=reset_node.session_id,
            session_key=SESSION_KEY,
            message_id="message-current-reset-turn",
            role="user",
            content="/meta meta-paper-write -- start in the reset session",
            created_at=300,
            turn_context={"meta_control": control},
        )
        task = AgentTaskRecord(
            task_id="task-current-reset-turn",
            session_key=SESSION_KEY,
            agent_id="main",
            source_kind="webui",
            queue_mode="followup",
            run_kind="web_turn",
            status=AgentTaskStatus.QUEUED,
            created_at=300,
            updated_at=300,
            details={"metadata": {"meta_control": control}},
        )

        accepted = await storage.accept_turn(
            entry,
            expected_epoch=1,
            updated_at=300,
            task_record=task,
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id="current-reset-turn",
            request_fingerprint="sha256:current-reset-turn",
            session_node=reset_node,
            reset_from_session_id=SESSION_ID,
            meta_control_intent_id=current.intent_id,
        )

        assert accepted.replayed is False
        assert await storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=stale.correlation_id,
        ) is None
        preserved = await storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=current.correlation_id,
        )
        assert preserved is not None
        assert preserved.status == "accepted"
        assert preserved.accepted_task_id == task.task_id
        accepted_history = await storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=accepted_old.correlation_id,
        )
        assert accepted_history is not None
        assert accepted_history.status == "accepted"
        assert await storage.is_meta_launch_discarded(
            session_key=SESSION_KEY,
            client_request_id="stale-before-reset",
        )
        assert await storage.is_meta_launch_discarded(
            session_key=SESSION_KEY,
            client_request_id="accepted-before-atomic-reset",
        )
        assert not await storage.is_meta_launch_discarded(
            session_key=SESSION_KEY,
            client_request_id="current-reset-turn",
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_meta_control_recovery_quarantines_invalid_head_before_claiming_valid(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())

        async def accept_control(index: int) -> AgentTaskRecord:
            request_id = f"recovery-{index}"
            message_id = f"recovery-message-{index}"
            task_id = f"recovery-task-{index}"
            created_at = 200 + index
            intent, _ = await storage.stage_meta_control_intent(
                session_key=SESSION_KEY,
                control_kind="manual",
                correlation_id=f"request:{request_id}",
                meta_skill_name="meta-paper-write",
            )
            control = {
                "version": 1,
                "intent_id": intent.intent_id,
                "kind": "manual",
                "name": "meta-paper-write",
                "correlation_id": f"request:{request_id}",
            }
            entry = TranscriptEntry(
                session_id=SESSION_ID,
                session_key=SESSION_KEY,
                message_id=message_id,
                role="user",
                content=f"/meta meta-paper-write -- recovery {index}",
                created_at=created_at,
                turn_context={"meta_control": control},
            )
            task = AgentTaskRecord(
                task_id=task_id,
                session_key=SESSION_KEY,
                agent_id="main",
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.QUEUED,
                created_at=created_at,
                updated_at=created_at,
                details={
                    "metadata": {"meta_control": control},
                    "persisted_user_message_id": message_id,
                },
            )
            await storage.accept_turn(
                entry,
                expected_epoch=0,
                updated_at=created_at,
                task_record=task,
                source_scope="webui",
                request_session_key=SESSION_KEY,
                client_request_id=request_id,
                request_fingerprint=f"sha256:{request_id}",
                meta_control_intent_id=intent.intent_id,
            )
            return task

        invalid = await accept_control(0)
        valid = await accept_control(1)
        assert await storage.mark_abandoned_agent_tasks(now_ms=300) == 2
        await storage.conn.execute(
            "UPDATE agent_tasks SET details = '{}' WHERE task_id = ?",
            (invalid.task_id,),
        )
        await storage.conn.commit()

        claimed = await storage.claim_recoverable_meta_control_tasks(limit=1)

        assert [item.task.task_id for item in claimed] == [valid.task_id]
        quarantined = await storage.get_agent_task(invalid.task_id)
        assert quarantined is not None
        assert quarantined.status == AgentTaskStatus.ABANDONED
        assert quarantined.terminal_reason == "meta_control_recovery_invalid"
        assert quarantined.error_class == "MetaControlRecoveryInvalid"
        assert await storage.claim_recoverable_meta_control_tasks(limit=1) == []
    finally:
        await storage.close()


@pytest.mark.parametrize(
    "terminal_status",
    [
        SessionStatus.DONE,
        SessionStatus.FAILED,
        SessionStatus.KILLED,
        SessionStatus.TIMEOUT,
    ],
)
@pytest.mark.asyncio
async def test_meta_control_recovery_never_reopens_terminal_session(
    tmp_path,
    terminal_status: SessionStatus,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        request_id = f"terminal-{terminal_status.value}"
        intent, _ = await storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
            meta_skill_name="meta-paper-write",
        )
        control = {
            "version": 1,
            "intent_id": intent.intent_id,
            "kind": "manual",
            "name": "meta-paper-write",
            "correlation_id": f"request:{request_id}",
        }
        message_id = f"message-{terminal_status.value}"
        task_id = f"task-{terminal_status.value}"
        await storage.accept_turn(
            TranscriptEntry(
                session_id=SESSION_ID,
                session_key=SESSION_KEY,
                message_id=message_id,
                role="user",
                content="/meta meta-paper-write -- terminal recovery",
                created_at=200,
                turn_context={"meta_control": control},
            ),
            expected_epoch=0,
            updated_at=200,
            task_record=AgentTaskRecord(
                task_id=task_id,
                session_key=SESSION_KEY,
                agent_id="main",
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.QUEUED,
                created_at=200,
                updated_at=200,
                details={
                    "metadata": {"meta_control": control},
                    "persisted_user_message_id": message_id,
                },
            ),
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id=request_id,
            request_fingerprint=f"sha256:{request_id}",
            meta_control_intent_id=intent.intent_id,
        )
        terminal = await storage.get_session(SESSION_KEY)
        assert terminal is not None
        terminal.status = terminal_status
        terminal.ended_at = 250
        terminal.runtime_ms = 150
        await storage.upsert_session(terminal)

        assert await storage.mark_abandoned_agent_tasks(now_ms=300) == 1
        abandoned = await storage.get_agent_task(task_id)
        assert abandoned is not None
        assert abandoned.status == AgentTaskStatus.ABANDONED
        assert abandoned.terminal_reason == "process_restart"

        # Databases opened once by the buggy build may already carry the
        # recovery marker. The claim path must reject those rows independently
        # rather than trusting only the current restart-marking pass.
        await storage.conn.execute(
            "UPDATE agent_tasks SET terminal_reason = ? WHERE task_id = ?",
            ("meta_control_restart_before_start", task_id),
        )
        await storage.conn.commit()

        assert await storage.claim_recoverable_meta_control_tasks() == []
        preserved = await storage.get_session(SESSION_KEY)
        assert preserved is not None
        assert preserved.status == terminal_status
        assert preserved.ended_at == 250
        assert preserved.runtime_ms == 150
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_commits_message_session_task_and_receipt_together(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())

        result = await _accept_turn(
            storage,
            message_id="message-one",
            task_id="task-one",
        )

        transcript = await storage.get_transcript(SESSION_ID)
        session = await storage.get_session(SESSION_KEY)
        task = await storage.get_agent_task("task-one")
        receipts = await _receipt_rows(storage)

        assert [entry.message_id for entry in transcript] == ["message-one"]
        assert session is not None
        assert session.updated_at == 200
        assert task is not None
        assert task.status == AgentTaskStatus.QUEUED
        assert task.details is not None
        assert task.details["persisted_user_message_id"] == "message-one"
        assert task.details["persisted_user_message_ids"] == ["message-one"]
        assert task.details["message_count"] == 1
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt["receipt_id"]
        assert receipt["accepted_at"] >= 200
        assert {
            key: receipt[key]
            for key in (
                "source_scope",
                "request_session_key",
                "client_request_id",
                "request_fingerprint",
                "accepted_session_key",
                "session_id",
                "message_id",
                "task_id",
                "schema_version",
            )
        } == {
            "source_scope": "webui",
            "request_session_key": SESSION_KEY,
            "client_request_id": "request-one",
            "request_fingerprint": "sha256:request-one",
            "accepted_session_key": SESSION_KEY,
            "session_id": SESSION_ID,
            "message_id": "message-one",
            "task_id": "task-one",
            "schema_version": 1,
        }
        assert _result_value(result, "message_id") == "message-one"
        assert _result_value(result, "task_id") == "task-one"
        assert _result_value(result, "session_id") == SESSION_ID
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_can_bind_receipt_to_existing_task_without_mutating_it(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "steer-receipt.db"))
    try:
        await storage.upsert_session(_session())
        running = _task("turn-running")
        running.status = AgentTaskStatus.RUNNING
        await storage.create_agent_task(running)

        accepted = await storage.accept_turn(
            _entry("message-steer", content="change direction"),
            expected_epoch=0,
            updated_at=250,
            task_record=None,
            receipt_task_id=running.task_id,
            source_scope="webui:steer.v2",
            request_session_key=SESSION_KEY,
            client_request_id="request-steer",
            request_fingerprint="sha256:request-steer",
        )
        replayed = await storage.accept_turn(
            _entry("unused-replay-entry", content="change direction"),
            expected_epoch=0,
            updated_at=251,
            task_record=None,
            receipt_task_id=running.task_id,
            source_scope="webui:steer.v2",
            request_session_key=SESSION_KEY,
            client_request_id="request-steer",
            request_fingerprint="sha256:request-steer",
        )

        task = await storage.get_agent_task(running.task_id)
        transcript = await storage.get_transcript(SESSION_ID)
        assert task is not None
        assert task.status == AgentTaskStatus.RUNNING
        assert accepted.receipt.task_id == running.task_id
        assert accepted.replayed is False
        assert replayed.receipt.message_id == "message-steer"
        assert replayed.replayed is True
        assert [entry.message_id for entry in transcript] == ["message-steer"]
        looked_up = await storage.get_canonical_transcript_entry(
            SESSION_ID,
            "message-steer",
        )
        assert looked_up is not None
        assert looked_up.content == "change direction"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_updates_session_origin_in_same_transaction(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        session = _session()
        session.origin = {
            "surface": "webchat",
            "sandbox_run_context": {
                "run_mode": "trusted",
                "run_mode_source": "operator_default",
            },
        }
        await storage.upsert_session(session)
        selected_origin = {
            "surface": "webchat",
            "sandbox_run_context": {
                "run_mode": "standard",
                "run_mode_source": "user",
            },
        }

        await storage.accept_turn(
            _entry("message-mode"),
            expected_epoch=0,
            updated_at=200,
            task_record=_task("task-mode"),
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id="request-mode",
            request_fingerprint="sha256:request-mode",
            session_updates={"origin": selected_origin},
        )

        persisted = await storage.get_session(SESSION_KEY)
        assert persisted is not None
        assert persisted.origin == selected_origin
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_rolls_back_session_origin_with_failed_task_insert(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        original_origin = {
            "sandbox_run_context": {
                "run_mode": "trusted",
                "run_mode_source": "operator_default",
            }
        }
        session = _session()
        session.origin = original_origin
        await storage.upsert_session(session)
        await storage.conn.execute(
            """
            CREATE TRIGGER fail_mode_task_insert
            BEFORE INSERT ON agent_tasks
            BEGIN
                SELECT RAISE(ABORT, 'injected mode acceptance failure');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="injected mode acceptance failure"):
            await storage.accept_turn(
                _entry("message-mode-failed"),
                expected_epoch=0,
                updated_at=200,
                task_record=_task("task-mode-failed"),
                source_scope="webui",
                request_session_key=SESSION_KEY,
                client_request_id="request-mode-failed",
                request_fingerprint="sha256:request-mode-failed",
                session_updates={
                    "origin": {
                        "sandbox_run_context": {
                            "run_mode": "full",
                            "run_mode_source": "user",
                        }
                    }
                },
            )

        persisted = await storage.get_session(SESSION_KEY)
        assert persisted is not None
        assert persisted.origin == original_origin
        assert await storage.count_transcript_entries(SESSION_ID) == 0
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_table", ["agent_tasks", "turn_ingress_receipts"])
async def test_accept_turn_rolls_back_every_write_when_an_insert_fails(
    tmp_path,
    failing_table: str,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / f"{failing_table}.db"))
    try:
        await storage.upsert_session(_session())
        trigger_name = f"fail_acceptance_insert_{failing_table}"
        await storage.conn.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE INSERT ON {failing_table}
            BEGIN
                SELECT RAISE(ABORT, 'injected acceptance failure');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="injected acceptance failure"):
            await _accept_turn(
                storage,
                message_id="message-failed",
                task_id="task-failed",
            )

        session = await storage.get_session(SESSION_KEY)
        assert session is not None
        assert session.updated_at == 100
        assert await storage.count_transcript_entries(SESSION_ID) == 0
        assert await storage.get_agent_task("task-failed") is None
        assert await _row_count(storage, "turn_ingress_receipts") == 0
        assert storage.conn.in_transaction is False
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_replays_same_request_without_duplicate_side_effects(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        first = await _accept_turn(
            storage,
            message_id="message-original",
            task_id="task-original",
        )

        replay = await _accept_turn(
            storage,
            message_id="message-prospective-retry",
            task_id="task-prospective-retry",
            updated_at=300,
        )

        session = await storage.get_session(SESSION_KEY)
        assert session is not None
        assert session.updated_at == 200
        assert await storage.count_transcript_entries(SESSION_ID) == 1
        assert await _row_count(storage, "agent_tasks") == 1
        assert await _row_count(storage, "turn_ingress_receipts") == 1
        assert await storage.get_agent_task("task-prospective-retry") is None
        assert _result_value(replay, "receipt_id") == _result_value(first, "receipt_id")
        assert _result_value(replay, "message_id") == "message-original"
        assert _result_value(replay, "task_id") == "task-original"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_fresh_webui_accept_turn_seeds_immutable_usage_baseline(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.initialize_usage_ledger(1)
        first = await storage.accept_turn(
            _entry("message-create"),
            expected_epoch=0,
            updated_at=200,
            task_record=_task("task-create"),
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id="request-create",
            request_fingerprint="sha256:request-create",
            session_node=_session(),
        )
        baseline = (await storage.list_usage_legacy_baselines())[0]
        assert first.replayed is False
        assert (baseline.session_id, baseline.session_epoch) == (SESSION_ID, 0)
        assert baseline.captured_at_ms > 1
        assert (baseline.input_tokens, baseline.output_tokens, baseline.total_tokens) == (0, 0, 0)

        replay = await storage.accept_turn(
            _entry("message-create-prospective-retry", created_at=300),
            expected_epoch=0,
            updated_at=300,
            task_record=_task("task-create-prospective-retry", updated_at=300),
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id="request-create",
            request_fingerprint="sha256:request-create",
            session_node=_session(updated_at=300),
        )
        assert replay.replayed is True
        assert await storage.list_usage_legacy_baselines() == [baseline]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_atomic_accept_turn_reset_seeds_new_generation_usage_baseline(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        old = _session()
        old.input_tokens = 12
        old.output_tokens = 4
        old.total_tokens = 16
        await storage.upsert_session(old)
        await storage.initialize_usage_ledger(150)
        old_baseline = (await storage.list_usage_legacy_baselines())[0]

        reset = old.model_copy(deep=True)
        reset.session_id = "session-after-reset"
        reset.epoch = 1
        reset.updated_at = 300
        reset.input_tokens = 0
        reset.output_tokens = 0
        reset.total_tokens = 0
        await storage.accept_turn(
            TranscriptEntry(
                session_id=reset.session_id,
                session_key=SESSION_KEY,
                message_id="message-after-reset",
                role="user",
                content="start over",
                created_at=300,
            ),
            expected_epoch=1,
            updated_at=300,
            task_record=_task("task-after-reset", updated_at=300),
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id="request-after-reset",
            request_fingerprint="sha256:request-after-reset",
            session_node=reset,
            reset_from_session_id=SESSION_ID,
        )

        baselines = await storage.list_usage_legacy_baselines()
        assert baselines[0] == old_baseline
        assert {(row.session_id, row.session_epoch) for row in baselines} == {
            (SESSION_ID, 0),
            (reset.session_id, 1),
        }
        reset_baseline = next(row for row in baselines if row.session_id == reset.session_id)
        assert (
            reset_baseline.input_tokens,
            reset_baseline.output_tokens,
            reset_baseline.total_tokens,
        ) == (0, 0, 0)
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_atomic_fork_accept_turn_seeds_zero_usage_baseline(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    child_key = "agent:main:webchat:fork-child"
    child_id = "session-fork-child"
    try:
        await storage.initialize_usage_ledger(1)
        child = SessionNode(
            session_key=child_key,
            session_id=child_id,
            created_at=100,
            updated_at=100,
            forked_from_parent=True,
        )
        inherited = TranscriptEntry(
            session_id=child_id,
            session_key=child_key,
            message_id="inherited-parent-message",
            role="assistant",
            content="parent answer",
            created_at=50,
        )
        await storage.accept_turn(
            TranscriptEntry(
                session_id=child_id,
                session_key=child_key,
                message_id="fork-user-message",
                role="user",
                content="edit from here",
                created_at=100,
            ),
            expected_epoch=0,
            updated_at=100,
            task_record=None,
            source_scope="webui",
            request_session_key=child_key,
            client_request_id="request-fork-child",
            request_fingerprint="sha256:request-fork-child",
            session_node=child,
            initial_transcript_entries=(inherited,),
        )

        baseline = (await storage.list_usage_legacy_baselines())[0]
        assert (baseline.session_id, baseline.session_epoch) == (child_id, 0)
        assert (baseline.input_tokens, baseline.output_tokens, baseline.total_tokens) == (0, 0, 0)
        assert [
            entry.message_id for entry in await storage.get_transcript(child_id)
        ] == ["inherited-parent-message", "fork-user-message"]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_fresh_accept_turn_rolls_back_usage_baseline_with_later_failure(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.initialize_usage_ledger(1)
        await storage.conn.execute(
            """
            CREATE TRIGGER fail_fresh_acceptance_receipt
            BEFORE INSERT ON turn_ingress_receipts
            BEGIN
                SELECT RAISE(ABORT, 'injected fresh acceptance failure');
            END
            """
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="injected fresh acceptance failure",
        ):
            await storage.accept_turn(
                _entry("message-failed-create"),
                expected_epoch=0,
                updated_at=200,
                task_record=_task("task-failed-create"),
                source_scope="webui",
                request_session_key=SESSION_KEY,
                client_request_id="request-failed-create",
                request_fingerprint="sha256:request-failed-create",
                session_node=_session(),
            )

        assert await storage.get_session(SESSION_KEY) is None
        assert await storage.list_usage_legacy_baselines() == []
        assert await storage.count_transcript_entries(SESSION_ID) == 0
        assert await storage.get_agent_task("task-failed-create") is None
        assert await _row_count(storage, "turn_ingress_receipts") == 0
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_collects_into_existing_task_in_the_same_transaction(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        existing = _task("task-collect")
        existing.queue_mode = "collect"
        existing.details = {
            "message_count": 1,
            "persisted_user_message_id": "message-first",
            "persisted_user_message_ids": ["message-first"],
            "fresh_user_session": True,
            "existing_only": "preserved",
        }
        await storage.create_agent_task(existing)

        collected = _task("task-collect", updated_at=300)
        collected.queue_mode = "collect"
        collected.details = {
            "collected": True,
            "message_count": 2,
            "persisted_user_message_id": "message-first",
            "persisted_user_message_ids": [
                "message-first",
                "message-collected",
            ],
        }
        result = await storage.accept_turn(
            _entry("message-collected", content="second", created_at=300),
            expected_epoch=0,
            updated_at=300,
            task_record=collected,
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id="request-collect",
            request_fingerprint="sha256:request-collect",
            merge_into_task=True,
        )

        task = await storage.get_agent_task("task-collect")
        assert task is not None
        assert task.details is not None
        assert task.details["collected"] is True
        assert task.details["message_count"] == 2
        assert task.details["persisted_user_message_id"] == "message-first"
        assert task.details["persisted_user_message_ids"] == [
            "message-first",
            "message-collected",
        ]
        assert task.details["fresh_user_session"] is True
        assert task.details["existing_only"] == "preserved"
        assert [
            entry.message_id for entry in await storage.get_transcript(SESSION_ID)
        ] == ["message-collected"]
        assert await _row_count(storage, "agent_tasks") == 1
        assert await _row_count(storage, "turn_ingress_receipts") == 1
        assert _result_value(result, "task_id") == "task-collect"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_failed_collected_acceptance_rolls_back_task_details_and_message(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        existing = _task("task-collect")
        existing.queue_mode = "collect"
        original_details = {
            "message_count": 1,
            "persisted_user_message_id": "message-first",
            "persisted_user_message_ids": ["message-first"],
        }
        existing.details = original_details
        await storage.create_agent_task(existing)
        await storage.conn.execute(
            """
            CREATE TRIGGER fail_collected_receipt
            BEFORE INSERT ON turn_ingress_receipts
            BEGIN
                SELECT RAISE(ABORT, 'injected collected receipt failure');
            END
            """
        )
        collected = _task("task-collect", updated_at=300)
        collected.queue_mode = "collect"
        collected.details = {"collected": True, "message_count": 2}

        with pytest.raises(sqlite3.IntegrityError, match="collected receipt failure"):
            await storage.accept_turn(
                _entry("message-collected", content="second", created_at=300),
                expected_epoch=0,
                updated_at=300,
                task_record=collected,
                source_scope="webui",
                request_session_key=SESSION_KEY,
                client_request_id="request-collect",
                request_fingerprint="sha256:request-collect",
                merge_into_task=True,
            )

        task = await storage.get_agent_task("task-collect")
        assert task is not None
        assert task.details == original_details
        assert await storage.get_transcript(SESSION_ID) == []
        assert await _row_count(storage, "turn_ingress_receipts") == 0
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_rejects_request_id_reuse_with_a_different_fingerprint(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        await _accept_turn(
            storage,
            message_id="message-original",
            task_id="task-original",
        )

        with pytest.raises(Exception) as caught:
            await _accept_turn(
                storage,
                message_id="message-conflict",
                task_id="task-conflict",
                fingerprint="sha256:different-payload",
                updated_at=300,
            )

        assert caught.value.__class__.__name__ == "TurnIngressConflictError"
        session = await storage.get_session(SESSION_KEY)
        assert session is not None
        assert session.updated_at == 200
        assert await storage.count_transcript_entries(SESSION_ID) == 1
        assert await _row_count(storage, "agent_tasks") == 1
        assert await _row_count(storage, "turn_ingress_receipts") == 1
        assert await storage.get_agent_task("task-conflict") is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_rechecks_project_guard_in_transaction(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        node = _session()
        node.workspace_id = project.workspace_id
        guard = ProjectWorkspaceGuard(
            project.workspace_id,
            project.path,
            project.path_key,
        )
        await storage.remove_project_workspace(project.workspace_id)

        with pytest.raises(ProjectWorkspaceStateError) as raised:
            await storage.accept_turn(
                _entry("guarded-message"),
                expected_epoch=0,
                updated_at=200,
                task_record=None,
                source_scope="web:test",
                request_session_key=node.session_key,
                client_request_id="req-guarded",
                request_fingerprint="sha256:guarded",
                session_node=node,
                workspace_guard=guard,
            )
        assert raised.value.reason == "removed"
        assert await storage.get_session(node.session_key) is None
        assert await storage.get_turn_ingress_receipt(
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="req-guarded",
        ) is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_rejects_project_binding_mismatch(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        first_path = tmp_path / "first"
        second_path = tmp_path / "second"
        first_path.mkdir()
        second_path.mkdir()
        first = await storage.create_or_restore_project_workspace(
            path=str(first_path.resolve()),
            path_key=str(first_path.resolve()),
            display_name="first",
            trusted_at=1,
        )
        second = await storage.create_or_restore_project_workspace(
            path=str(second_path.resolve()),
            path_key=str(second_path.resolve()),
            display_name="second",
            trusted_at=1,
        )
        node = _session()
        node.workspace_id = first.workspace_id
        guard = ProjectWorkspaceGuard(
            second.workspace_id,
            second.path,
            second.path_key,
        )

        with pytest.raises(ProjectWorkspaceStateError) as raised:
            await storage.accept_turn(
                _entry("binding-mismatch"),
                expected_epoch=0,
                updated_at=200,
                task_record=None,
                source_scope="web:test",
                request_session_key=node.session_key,
                client_request_id="req-binding-mismatch",
                request_fingerprint="sha256:binding-mismatch",
                session_node=node,
                workspace_guard=guard,
            )

        assert raised.value.reason == "binding_changed"
        assert await storage.get_session(node.session_key) is None
        assert await storage.get_turn_ingress_receipt(
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="req-binding-mismatch",
        ) is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_requires_guard_for_bound_session(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        node = _session()
        node.workspace_id = project.workspace_id

        with pytest.raises(ProjectWorkspaceStateError) as raised:
            await storage.accept_turn(
                _entry("missing-guard"),
                expected_epoch=0,
                updated_at=200,
                task_record=None,
                source_scope="web:test",
                request_session_key=node.session_key,
                client_request_id="req-missing-guard",
                request_fingerprint="sha256:missing-guard",
                session_node=node,
            )

        assert raised.value.reason == "guard_required"
        assert await storage.get_session(node.session_key) is None
        assert await storage.get_turn_ingress_receipt(
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="req-missing-guard",
        ) is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_without_task_persists_nullable_receipt(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        node = _session()
        node.workspace_id = project.workspace_id
        guard = ProjectWorkspaceGuard(
            project.workspace_id,
            project.path,
            project.path_key,
        )

        accepted = await storage.accept_turn(
            _entry("taskless-message"),
            expected_epoch=0,
            updated_at=200,
            task_record=None,
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="req-taskless",
            request_fingerprint="sha256:taskless",
            session_node=node,
            workspace_guard=guard,
        )

        assert accepted.replayed is False
        assert accepted.receipt.task_id is None
        assert [
            item.message_id for item in await storage.get_transcript(node.session_id)
        ] == ["taskless-message"]
        receipt = await storage.get_turn_ingress_receipt(
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="req-taskless",
        )
        assert receipt is not None
        assert receipt.receipt.task_id is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_replays_before_removed_project_guard(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        node = _session()
        node.workspace_id = project.workspace_id
        guard = ProjectWorkspaceGuard(
            project.workspace_id,
            project.path,
            project.path_key,
        )
        first = await storage.accept_turn(
            _entry("original-project-message"),
            expected_epoch=0,
            updated_at=200,
            task_record=None,
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="req-project-replay",
            request_fingerprint="sha256:project-replay",
            session_node=node,
            workspace_guard=guard,
        )
        await storage.remove_project_workspace(project.workspace_id)

        replay = await storage.accept_turn(
            _entry("prospective-replay"),
            expected_epoch=0,
            updated_at=300,
            task_record=None,
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="req-project-replay",
            request_fingerprint="sha256:project-replay",
            session_node=node,
            workspace_guard=guard,
        )

        assert replay.replayed is True
        assert replay.receipt.receipt_id == first.receipt.receipt_id
        with pytest.raises(TurnIngressConflictError):
            await storage.accept_turn(
                _entry("conflicting-replay"),
                expected_epoch=0,
                updated_at=300,
                task_record=None,
                source_scope="web:test",
                request_session_key=node.session_key,
                client_request_id="req-project-replay",
                request_fingerprint="sha256:changed",
                session_node=node,
                workspace_guard=guard,
            )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_busy_timeout_has_no_partial_side_effects(tmp_path) -> None:
    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    locker: sqlite3.Connection | None = None
    try:
        await storage.upsert_session(_session())
        storage._busy_budget_seconds = 0.0
        locker = sqlite3.connect(str(db_path), timeout=0.1, isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")

        with pytest.raises(StorageBusyError):
            await _accept_turn(
                storage,
                message_id="message-busy",
                task_id="task-busy",
            )

        locker.execute("ROLLBACK")
        locker.close()
        locker = None

        session = await storage.get_session(SESSION_KEY)
        assert session is not None
        assert session.updated_at == 100
        assert await storage.count_transcript_entries(SESSION_ID) == 0
        assert await storage.get_agent_task("task-busy") is None
        assert await _row_count(storage, "turn_ingress_receipts") == 0
        assert storage.conn.in_transaction is False
    finally:
        if locker is not None:
            locker.execute("ROLLBACK")
            locker.close()
        await storage.close()


@pytest.mark.asyncio
async def test_project_accept_after_history_delete_commit_remains_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "sessions.db"
    deleting_storage = await SessionStorage.open(str(db_path))
    accepting_storage = await SessionStorage.open(str(db_path))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await deleting_storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        old = SessionNode(
            session_key="agent:main:webchat:history-old",
            workspace_id=project.workspace_id,
            created_at=100,
            updated_at=100,
        )
        await deleting_storage.upsert_session(old)
        new = SessionNode(
            session_key="agent:main:webchat:history-new",
            workspace_id=project.workspace_id,
            created_at=200,
            updated_at=200,
        )
        entry = TranscriptEntry(
            session_id=new.session_id,
            session_key=new.session_key,
            message_id="history-new-message",
            role="user",
            content="new history",
            created_at=200,
        )
        guard = ProjectWorkspaceGuard(
            project.workspace_id,
            project.path,
            project.path_key,
        )
        delete_entered = asyncio.Event()
        release_delete = asyncio.Event()
        accept_begin_attempted = asyncio.Event()
        original_delete_rows = deleting_storage._delete_session_rows
        original_begin = accepting_storage._begin_immediate

        async def paused_delete_rows(
            conn: Any,
            session: SessionNode,
        ) -> None:
            delete_entered.set()
            await release_delete.wait()
            await original_delete_rows(conn, session)

        async def observed_accept_begin(
            conn: Any,
            operation: str,
            deadline: float,
            started: float,
        ) -> None:
            if operation == "accept_turn":
                accept_begin_attempted.set()
            await original_begin(conn, operation, deadline, started)

        monkeypatch.setattr(
            deleting_storage,
            "_delete_session_rows",
            paused_delete_rows,
        )
        monkeypatch.setattr(
            accepting_storage,
            "_begin_immediate",
            observed_accept_begin,
        )
        deleting = asyncio.create_task(
            deleting_storage.delete_project_workspace_sessions(
                project.workspace_id,
                expected_session_keys=[old.session_key],
            )
        )
        await asyncio.wait_for(delete_entered.wait(), timeout=2)
        accepting = asyncio.create_task(
            accepting_storage.accept_turn(
                entry,
                expected_epoch=0,
                updated_at=200,
                task_record=None,
                source_scope="web:test",
                request_session_key=new.session_key,
                client_request_id="history-new-request",
                request_fingerprint="sha256:history-new-request",
                session_node=new,
                workspace_guard=guard,
            )
        )
        await asyncio.wait_for(accept_begin_attempted.wait(), timeout=2)
        await asyncio.sleep(0)
        assert accepting.done() is False
        release_delete.set()

        assert await deleting == [old.session_key]
        await accepting
        assert await accepting_storage.get_session(old.session_key) is None
        assert await accepting_storage.get_session(new.session_key) is not None
        assert [
            item.message_id
            for item in await accepting_storage.get_transcript(new.session_id)
        ] == ["history-new-message"]
    finally:
        await accepting_storage.close()
        await deleting_storage.close()


@pytest.mark.asyncio
async def test_project_accept_committed_before_history_delete_is_in_deleted_snapshot(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        node = SessionNode(
            session_key="agent:main:webchat:history-before-delete",
            workspace_id=project.workspace_id,
            created_at=100,
            updated_at=100,
        )
        guard = ProjectWorkspaceGuard(
            project.workspace_id,
            project.path,
            project.path_key,
        )
        await storage.accept_turn(
            TranscriptEntry(
                session_id=node.session_id,
                session_key=node.session_key,
                message_id="history-before-delete-message",
                role="user",
                content="old history",
                created_at=100,
            ),
            expected_epoch=0,
            updated_at=100,
            task_record=None,
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="history-before-delete-request",
            request_fingerprint="sha256:history-before-delete-request",
            session_node=node,
            workspace_guard=guard,
        )

        snapshot = await storage.list_project_workspace_session_keys(
            project.workspace_id
        )
        assert await storage.delete_project_workspace_sessions(
            project.workspace_id,
            expected_session_keys=snapshot,
        ) == [node.session_key]

        assert await storage.get_session(node.session_key) is None
        assert await storage.get_transcript(node.session_id) == []
        assert (
            await storage.get_turn_ingress_receipt(
                source_scope="web:test",
                request_session_key=node.session_key,
                client_request_id="history-before-delete-request",
            )
            is None
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_project_remove_commit_serializes_before_accept_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "sessions.db"
    removing_storage = await SessionStorage.open(str(db_path))
    accepting_storage = await SessionStorage.open(str(db_path))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await removing_storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        node = SessionNode(
            session_key="agent:main:webchat:removed-before-accept",
            workspace_id=project.workspace_id,
            created_at=100,
            updated_at=100,
        )
        guard = ProjectWorkspaceGuard(
            project.workspace_id,
            project.path,
            project.path_key,
        )
        commit_entered = asyncio.Event()
        release_commit = asyncio.Event()
        accept_begin_attempted = asyncio.Event()
        original_commit = removing_storage._commit_transaction
        original_begin = accepting_storage._begin_immediate

        async def gated_commit(
            conn: Any,
            operation: str,
            deadline: float,
            started: float,
        ) -> None:
            if operation == "remove_project_workspace":
                commit_entered.set()
                await release_commit.wait()
            await original_commit(conn, operation, deadline, started)

        async def observed_accept_begin(
            conn: Any,
            operation: str,
            deadline: float,
            started: float,
        ) -> None:
            if operation == "accept_turn":
                accept_begin_attempted.set()
            await original_begin(conn, operation, deadline, started)

        monkeypatch.setattr(removing_storage, "_commit_transaction", gated_commit)
        monkeypatch.setattr(
            accepting_storage,
            "_begin_immediate",
            observed_accept_begin,
        )
        removing = asyncio.create_task(
            removing_storage.remove_project_workspace(project.workspace_id)
        )
        await asyncio.wait_for(commit_entered.wait(), timeout=2)
        accepting = asyncio.create_task(
            accepting_storage.accept_turn(
                TranscriptEntry(
                    session_id=node.session_id,
                    session_key=node.session_key,
                    message_id="removed-before-accept-message",
                    role="user",
                    content="must not persist",
                    created_at=100,
                ),
                expected_epoch=0,
                updated_at=100,
                task_record=None,
                source_scope="web:test",
                request_session_key=node.session_key,
                client_request_id="removed-before-accept-request",
                request_fingerprint="sha256:removed-before-accept-request",
                session_node=node,
                workspace_guard=guard,
            )
        )
        await asyncio.wait_for(accept_begin_attempted.wait(), timeout=2)
        await asyncio.sleep(0)
        assert accepting.done() is False
        release_commit.set()
        await removing

        with pytest.raises(ProjectWorkspaceStateError) as raised:
            await accepting
        assert raised.value.reason == "removed"
        assert await accepting_storage.get_session(node.session_key) is None
        assert await accepting_storage.get_transcript(node.session_id) == []
        assert (
            await accepting_storage.get_turn_ingress_receipt(
                source_scope="web:test",
                request_session_key=node.session_key,
                client_request_id="removed-before-accept-request",
            )
            is None
        )
    finally:
        await accepting_storage.close()
        await removing_storage.close()
