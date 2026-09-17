"""Durability and lifecycle contracts for unaccepted MetaSkill requests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import opensquilla.session.storage as storage_module
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionNode,
    TranscriptEntry,
)
from opensquilla.session.storage import (
    MetaLaunchDraftCapacityError,
    MetaLaunchDraftConflictError,
    MetaLaunchDraftDiscardedError,
    SessionStorage,
)

SESSION_KEY = "agent:main:webchat:durable-meta-draft"
SESSION_ID = "durable-meta-draft-session"
REQUEST_ID = "durable-meta-draft-request"
LAUNCH_TEXT = "/meta meta-paper-write -- Write the full paper with cited sources"


def _session() -> SessionNode:
    return SessionNode(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        agent_id="main",
        created_at=100,
        updated_at=100,
        epoch=0,
    )


@pytest.mark.asyncio
async def test_draft_survives_reopen_and_acceptance_consumes_it_atomically(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    try:
        await storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=REQUEST_ID,
            meta_skill_name="meta-paper-write",
            launch_text=LAUNCH_TEXT,
        )
    finally:
        await storage.close()

    reopened = await SessionStorage.open(str(db_path))
    try:
        recovered = await reopened.list_meta_launch_drafts(agent_id="main")
        assert [(draft.client_request_id, draft.launch_text) for draft in recovered] == [
            (REQUEST_ID, LAUNCH_TEXT)
        ]

        await reopened.upsert_session(_session())
        intent, _ = await reopened.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{REQUEST_ID}",
            meta_skill_name="meta-paper-write",
        )
        control = {
            "version": 1,
            "intent_id": intent.intent_id,
            "kind": "manual",
            "name": "meta-paper-write",
            "correlation_id": intent.correlation_id,
        }
        entry = TranscriptEntry(
            session_id=SESSION_ID,
            session_key=SESSION_KEY,
            message_id="durable-meta-draft-message",
            role="user",
            content=LAUNCH_TEXT,
            created_at=200,
            turn_context={"meta_control": control},
        )
        task = AgentTaskRecord(
            task_id="durable-meta-draft-task",
            session_key=SESSION_KEY,
            agent_id="main",
            source_kind="webui",
            queue_mode="followup",
            run_kind="web_turn",
            status=AgentTaskStatus.QUEUED,
            created_at=200,
            updated_at=200,
            details={"metadata": {"meta_control": control}},
        )

        await reopened.accept_turn(
            entry,
            expected_epoch=0,
            updated_at=200,
            task_record=task,
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id=REQUEST_ID,
            request_fingerprint="sha256:durable-meta-draft",
            meta_control_intent_id=intent.intent_id,
        )

        assert await reopened.list_meta_launch_drafts(session_key=SESSION_KEY) == []
        assert not await reopened.discard_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=REQUEST_ID,
        )
        accepted_intent = await reopened.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{REQUEST_ID}",
        )
        assert accepted_intent is not None
        assert accepted_intent.status == "accepted"
        transcript = await reopened.get_transcript(SESSION_ID)
        assert transcript[0].content == LAUNCH_TEXT
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_reset_and_delete_remove_session_drafts_including_provisional_key(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        await storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id="before-reset",
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- reset me",
        )
        assert await storage.advance_reset_epoch(SESSION_KEY) == 1
        assert await storage.list_meta_launch_drafts(session_key=SESSION_KEY) == []
        with pytest.raises(MetaLaunchDraftDiscardedError):
            await storage.stage_meta_launch_draft(
                session_key=SESSION_KEY,
                client_request_id="before-reset",
                meta_skill_name="meta-paper-write",
                launch_text="/meta meta-paper-write -- reset me",
            )

        await storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id="before-delete",
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- delete me",
        )
        await storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id="discarded-before-delete",
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- discard before deleting",
        )
        assert await storage.discard_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id="discarded-before-delete",
        )
        await storage.delete_session(SESSION_KEY)
        assert await storage.list_meta_launch_drafts(session_key=SESSION_KEY) == []
        async with storage.conn.execute(
            """
            SELECT client_request_id
            FROM meta_launch_discard_tombstones
            WHERE session_key = ?
            """,
            (SESSION_KEY,),
        ) as cur:
            tombstones = {str(row[0]) for row in await cur.fetchall()}
        assert tombstones == {
            "before-reset",
            "before-delete",
            "discarded-before-delete",
        }
        for request_id, launch_text in (
            ("before-delete", "/meta meta-paper-write -- delete me"),
            (
                "discarded-before-delete",
                "/meta meta-paper-write -- discard before deleting",
            ),
        ):
            with pytest.raises(MetaLaunchDraftDiscardedError):
                await storage.stage_meta_launch_draft(
                    session_key=SESSION_KEY,
                    client_request_id=request_id,
                    meta_skill_name="meta-paper-write",
                    launch_text=launch_text,
                )

        provisional = "agent:main:webchat:provisional-only"
        await storage.stage_meta_launch_draft(
            session_key=provisional,
            client_request_id="provisional-delete",
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- never accepted",
        )
        await storage.promote_meta_launch_draft(
            session_key=provisional,
            client_request_id="provisional-delete",
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- never accepted",
        )
        await storage.delete_session(provisional)
        assert await storage.list_meta_launch_drafts(session_key=provisional) == []
        assert await storage.get_meta_control_intent(
            session_key=provisional,
            control_kind="manual",
            correlation_id="request:provisional-delete",
        ) is None
        with pytest.raises(MetaLaunchDraftDiscardedError):
            await storage.stage_meta_launch_draft(
                session_key=provisional,
                client_request_id="provisional-delete",
                meta_skill_name="meta-paper-write",
                launch_text="/meta meta-paper-write -- never accepted",
            )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_draft_identity_ttl_and_capacity_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_module, "_META_LAUNCH_DRAFT_PER_SESSION_LIMIT", 2)
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        first, disposition = await storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id="request-1",
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- first",
        )
        replayed, replay_disposition = await storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id="request-1",
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- first",
        )
        assert replayed.draft_id == first.draft_id
        assert (disposition, replay_disposition) == ("stamped", "replayed")

        with pytest.raises(MetaLaunchDraftConflictError):
            await storage.stage_meta_launch_draft(
                session_key=SESSION_KEY,
                client_request_id="request-1",
                meta_skill_name="meta-paper-write",
                launch_text="/meta meta-paper-write -- changed",
            )

        await storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id="request-2",
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- second",
        )
        with pytest.raises(MetaLaunchDraftCapacityError):
            await storage.stage_meta_launch_draft(
                session_key=SESSION_KEY,
                client_request_id="request-3",
                meta_skill_name="meta-paper-write",
                launch_text="/meta meta-paper-write -- third",
            )

        await storage.conn.execute(
            "UPDATE meta_launch_drafts SET expires_at = 1 WHERE draft_id = ?",
            (first.draft_id,),
        )
        await storage.conn.commit()
        await storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id="request-3",
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- third",
        )
        recovered = await storage.list_meta_launch_drafts(session_key=SESSION_KEY)
        assert [draft.client_request_id for draft in recovered] == ["request-2", "request-3"]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_agent_recovery_uses_an_exact_agent_prefix_before_limit(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        # SQLite LIKE treats ``_`` as a wildcard. A lookalike agent created
        # first must not consume the bounded recovery page for ``main_test``.
        await storage.stage_meta_launch_draft(
            session_key="agent:mainXtest:webchat:lookalike",
            client_request_id="lookalike",
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- lookalike",
        )
        await storage.stage_meta_launch_draft(
            session_key="agent:main_test:webchat:exact",
            client_request_id="exact",
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- exact",
        )

        recovered = await storage.list_meta_launch_drafts(agent_id="main_test", limit=1)

        assert [draft.client_request_id for draft in recovered] == ["exact"]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_discarded_draft_cannot_be_promoted_after_slow_readiness_boundary(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=REQUEST_ID,
            meta_skill_name="meta-paper-write",
            launch_text=LAUNCH_TEXT,
        )
        assert await storage.discard_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=REQUEST_ID,
        )

        with pytest.raises(MetaLaunchDraftDiscardedError):
            await storage.promote_meta_launch_draft(
                session_key=SESSION_KEY,
                client_request_id=REQUEST_ID,
                meta_skill_name="meta-paper-write",
                launch_text=LAUNCH_TEXT,
            )
        assert await storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{REQUEST_ID}",
        ) is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_listing_physically_purges_expired_prompt_and_staged_control(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        draft, _ = await storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=REQUEST_ID,
            meta_skill_name="meta-paper-write",
            launch_text=LAUNCH_TEXT,
        )
        await storage.promote_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=REQUEST_ID,
            meta_skill_name="meta-paper-write",
            launch_text=LAUNCH_TEXT,
        )
        await storage.conn.execute(
            "UPDATE meta_launch_drafts SET expires_at = 1 WHERE draft_id = ?",
            (draft.draft_id,),
        )
        await storage.conn.commit()

        assert await storage.list_meta_launch_drafts(session_key=SESSION_KEY) == []
        async with storage.conn.execute(
            "SELECT COUNT(*) FROM meta_launch_drafts WHERE draft_id = ?",
            (draft.draft_id,),
        ) as cur:
            assert (await cur.fetchone())[0] == 0
        assert await storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{REQUEST_ID}",
        ) is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_discard_is_idempotent_after_committed_response_loss(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=REQUEST_ID,
            meta_skill_name="meta-paper-write",
            launch_text=LAUNCH_TEXT,
        )
        assert await storage.discard_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=REQUEST_ID,
        )
        assert await storage.discard_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=REQUEST_ID,
        )
    finally:
        await storage.close()


@pytest.mark.parametrize(
    ("session_key", "client_request_id"),
    (
        ("x" * 513, REQUEST_ID),
        (SESSION_KEY, "request id with spaces"),
        (SESSION_KEY, "x" * 257),
        (SESSION_KEY, LAUNCH_TEXT),
        (42, REQUEST_ID),
    ),
)
@pytest.mark.asyncio
async def test_invalid_discard_coordinates_never_create_tombstones(
    tmp_path: Path,
    session_key: object,
    client_request_id: object,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        assert not await storage.discard_meta_launch_draft(
            session_key=session_key,  # type: ignore[arg-type]
            client_request_id=client_request_id,  # type: ignore[arg-type]
        )
        async with storage.conn.execute(
            "SELECT COUNT(*) FROM meta_launch_discard_tombstones"
        ) as cur:
            assert (await cur.fetchone())[0] == 0
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_discard_tombstones_are_bounded_but_existing_ids_remain_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_module, "_META_LAUNCH_DISCARD_PER_SESSION_LIMIT", 2)
    monkeypatch.setattr(storage_module, "_META_LAUNCH_DISCARD_GLOBAL_LIMIT", 3)
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        for request_id in ("discard-1", "discard-2"):
            assert await storage.discard_meta_launch_draft(
                session_key=SESSION_KEY,
                client_request_id=request_id,
            )
        # A response-loss retry for an existing coordinate must still confirm
        # the terminal cancellation even when the session bucket is full.
        assert await storage.discard_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id="discard-1",
        )
        with pytest.raises(MetaLaunchDraftCapacityError):
            await storage.discard_meta_launch_draft(
                session_key=SESSION_KEY,
                client_request_id="discard-3",
            )

        assert await storage.discard_meta_launch_draft(
            session_key="agent:main:webchat:other-discard",
            client_request_id="discard-other",
        )
        with pytest.raises(MetaLaunchDraftCapacityError):
            await storage.discard_meta_launch_draft(
                session_key="agent:main:webchat:global-full",
                client_request_id="discard-global-full",
            )
        async with storage.conn.execute(
            "SELECT COUNT(*) FROM meta_launch_discard_tombstones"
        ) as cur:
            assert (await cur.fetchone())[0] == 3
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_cancel_before_stage_shares_capacity_with_live_drafts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_module, "_META_LAUNCH_DRAFT_PER_SESSION_LIMIT", 2)
    monkeypatch.setattr(storage_module, "_META_LAUNCH_DISCARD_PER_SESSION_LIMIT", 2)
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        for index in (1, 2):
            await storage.stage_meta_launch_draft(
                session_key=SESSION_KEY,
                client_request_id=f"live-draft-{index}",
                meta_skill_name="meta-paper-write",
                launch_text=f"/meta meta-paper-write -- live draft {index}",
            )

        with pytest.raises(MetaLaunchDraftCapacityError):
            await storage.discard_meta_launch_draft(
                session_key=SESSION_KEY,
                client_request_id="cancel-before-stage",
            )

        assert await storage.discard_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id="live-draft-1",
        )
        async with storage.conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM meta_launch_drafts WHERE session_key = ?)
                +
                (SELECT COUNT(*) FROM meta_launch_discard_tombstones
                 WHERE session_key = ?)
            """,
            (SESSION_KEY, SESSION_KEY),
        ) as cur:
            assert int((await cur.fetchone())[0]) == 2
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_legacy_manual_staging_respects_shared_session_capacity(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        for index in range(storage_module._META_LAUNCH_DISCARD_PER_SESSION_LIMIT):
            await storage.stage_meta_control_intent(
                session_key=SESSION_KEY,
                control_kind="manual",
                correlation_id=f"request:legacy-capacity-{index}",
                meta_skill_name="meta-paper-write",
            )

        with pytest.raises(MetaLaunchDraftCapacityError):
            await storage.stage_meta_control_intent(
                session_key=SESSION_KEY,
                control_kind="manual",
                correlation_id="request:legacy-capacity-overflow",
                meta_skill_name="meta-paper-write",
            )

        async with storage.conn.execute(
            "SELECT COUNT(*) FROM meta_control_intents WHERE session_key = ?",
            (SESSION_KEY,),
        ) as cur:
            assert int((await cur.fetchone())[0]) == 64
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_global_accepted_selector_is_ranked_and_bounded_in_sql(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    statements: list[str] = []
    try:
        intent, _ = await storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id="request:accepted-query-bound",
            meta_skill_name="meta-paper-write",
        )
        await storage.conn.execute(
            "UPDATE meta_control_intents SET status = 'accepted' WHERE intent_id = ?",
            (intent.intent_id,),
        )
        await storage.conn.commit()
        await storage.conn.set_trace_callback(statements.append)

        coordinates = await storage._select_live_meta_launch_coordinates(
            storage.conn,
            now_ms=storage_module._now_ms(),
            include_drafts=False,
            include_tombstones=False,
            include_staged=False,
        )

        assert (SESSION_KEY, "accepted-query-bound") in coordinates
        accepted_queries = [
            statement
            for statement in statements
            if "status = 'accepted'" in statement
        ]
        assert accepted_queries
        assert "ROW_NUMBER() OVER" in accepted_queries[-1]
        assert "LIMIT" in accepted_queries[-1]
    finally:
        await storage.conn.set_trace_callback(None)
        await storage.close()


@pytest.mark.asyncio
async def test_delete_succeeds_after_more_accepted_controls_than_tombstone_capacity(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        accepted_at = storage_module._now_ms()
        for index in range(65):
            request_id = f"accepted-before-delete-{index:02d}"
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
                "correlation_id": intent.correlation_id,
            }
            timestamp = accepted_at + index
            await storage.accept_turn(
                TranscriptEntry(
                    session_id=SESSION_ID,
                    session_key=SESSION_KEY,
                    message_id=f"accepted-message-{index:02d}",
                    role="user",
                    content=f"/meta meta-paper-write -- accepted {index}",
                    created_at=timestamp,
                    turn_context={"meta_control": control},
                ),
                expected_epoch=0,
                updated_at=timestamp,
                task_record=AgentTaskRecord(
                    task_id=f"accepted-task-{index:02d}",
                    session_key=SESSION_KEY,
                    agent_id="main",
                    source_kind="webui",
                    queue_mode="followup",
                    run_kind="web_turn",
                    status=AgentTaskStatus.QUEUED,
                    created_at=timestamp,
                    updated_at=timestamp,
                    details={"metadata": {"meta_control": control}},
                ),
                source_scope="webui",
                request_session_key=SESSION_KEY,
                client_request_id=request_id,
                request_fingerprint=f"sha256:{request_id}",
                meta_control_intent_id=intent.intent_id,
            )

        await storage.delete_session(SESSION_KEY)

        assert await storage.get_session(SESSION_KEY) is None
        async with storage.conn.execute(
            "SELECT COUNT(*) FROM meta_launch_discard_tombstones WHERE session_key = ?",
            (SESSION_KEY,),
        ) as cur:
            assert int((await cur.fetchone())[0]) == (
                storage_module._META_LAUNCH_ACCEPTED_PER_SESSION_LIMIT
            )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_delete_fences_legacy_staged_control_for_its_full_recovery_window(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    request_id = "old-legacy-request"
    try:
        intent, _ = await storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
            meta_skill_name="meta-paper-write",
        )
        eight_days_ago = storage_module._now_ms() - (8 * 24 * 60 * 60 * 1000)
        await storage.conn.execute(
            """
            UPDATE meta_control_intents SET created_at = ?, updated_at = ?
            WHERE intent_id = ?
            """,
            (eight_days_ago, eight_days_ago, intent.intent_id),
        )
        await storage.conn.commit()

        await storage.delete_session(SESSION_KEY)

        with pytest.raises(MetaLaunchDraftDiscardedError):
            await storage.stage_meta_control_intent(
                session_key=SESSION_KEY,
                control_kind="manual",
                correlation_id=f"request:{request_id}",
                meta_skill_name="meta-paper-write",
            )
    finally:
        await storage.close()


@pytest.mark.parametrize("boundary", ("reset", "delete"))
@pytest.mark.asyncio
async def test_session_boundary_converts_existing_coordinates_even_above_current_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        assert await storage.discard_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id="existing-terminal-request",
        )
        await storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id="request:pending-boundary-request",
            meta_skill_name="meta-paper-write",
        )
        # Simulate a limit reduction after both live coordinates were accepted.
        # A boundary converts existing coordinates; it must not reserve new ones.
        monkeypatch.setattr(storage_module, "_META_LAUNCH_DISCARD_PER_SESSION_LIMIT", 1)
        monkeypatch.setattr(storage_module, "_META_LAUNCH_DISCARD_GLOBAL_LIMIT", 1)

        if boundary == "reset":
            await storage.advance_reset_epoch(SESSION_KEY)
        else:
            await storage.delete_session(SESSION_KEY)

        session = await storage.get_session(SESSION_KEY)
        if boundary == "reset":
            assert session is not None
            assert session.epoch == 1
        else:
            assert session is None
        assert await storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id="request:pending-boundary-request",
        ) is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_expired_tombstones_outside_the_gc_page_do_not_consume_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_module, "_META_LAUNCH_DRAFT_GC_BATCH", 1)
    monkeypatch.setattr(storage_module, "_META_LAUNCH_DISCARD_PER_SESSION_LIMIT", 1)
    monkeypatch.setattr(storage_module, "_META_LAUNCH_DISCARD_GLOBAL_LIMIT", 1)
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.conn.executemany(
            """
            INSERT INTO meta_launch_discard_tombstones (
                session_key, client_request_id, created_at, expires_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                ("agent:main:webchat:older-expired", "old-1", 0, 1),
                (SESSION_KEY, "expired-in-target-session", 1, 2),
            ),
        )
        await storage.conn.commit()

        assert await storage.discard_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id="fresh-after-expiry",
        )
        async with storage.conn.execute(
            """
            SELECT COUNT(*) FROM meta_launch_discard_tombstones
            WHERE expires_at > 2
            """
        ) as cur:
            assert (await cur.fetchone())[0] == 1
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_discard_tombstone_survives_reopen_and_blocks_stale_restage(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    try:
        await storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=REQUEST_ID,
            meta_skill_name="meta-paper-write",
            launch_text=LAUNCH_TEXT,
        )
        assert await storage.discard_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=REQUEST_ID,
        )
    finally:
        await storage.close()

    reopened = await SessionStorage.open(str(db_path))
    try:
        async with reopened.conn.execute(
            """
            SELECT created_at, expires_at
            FROM meta_launch_discard_tombstones
            WHERE session_key = ? AND client_request_id = ?
            """,
            (SESSION_KEY, REQUEST_ID),
        ) as cur:
            marker = await cur.fetchone()
        assert marker is not None
        assert int(marker[1]) > int(marker[0])

        # A repeated discard after a lost response confirms cancellation but
        # does not turn a finite marker into indefinite retained state.
        assert await reopened.discard_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=REQUEST_ID,
        )
        async with reopened.conn.execute(
            """
            SELECT expires_at
            FROM meta_launch_discard_tombstones
            WHERE session_key = ? AND client_request_id = ?
            """,
            (SESSION_KEY, REQUEST_ID),
        ) as cur:
            replayed_marker = await cur.fetchone()
        assert replayed_marker is not None
        assert int(replayed_marker[0]) == int(marker[1])

        with pytest.raises(MetaLaunchDraftDiscardedError):
            await reopened.stage_meta_launch_draft(
                session_key=SESSION_KEY,
                client_request_id=REQUEST_ID,
                meta_skill_name="meta-paper-write",
                launch_text=LAUNCH_TEXT,
            )

        columns = {
            str(row[1])
            for row in await (
                await reopened.conn.execute(
                    "PRAGMA table_info(meta_launch_discard_tombstones)"
                )
            ).fetchall()
        }
        assert "launch_text" not in columns
        assert "meta_skill_name" not in columns

        # Markers are intentionally finite; normal staging becomes possible
        # again once retention cleanup has physically removed an expired row.
        await reopened.conn.execute(
            """
            UPDATE meta_launch_discard_tombstones
            SET expires_at = 1
            WHERE session_key = ? AND client_request_id = ?
            """,
            (SESSION_KEY, REQUEST_ID),
        )
        await reopened.conn.commit()
        draft, disposition = await reopened.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=REQUEST_ID,
            meta_skill_name="meta-paper-write",
            launch_text=LAUNCH_TEXT,
        )
        assert draft.client_request_id == REQUEST_ID
        assert disposition == "stamped"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_periodic_gc_physically_enforces_retention_while_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_module, "_META_LAUNCH_DRAFT_GC_INTERVAL_SECONDS", 0.01)
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        draft, _ = await storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=REQUEST_ID,
            meta_skill_name="meta-paper-write",
            launch_text=LAUNCH_TEXT,
        )
        await storage.conn.execute(
            "UPDATE meta_launch_drafts SET expires_at = 1 WHERE draft_id = ?",
            (draft.draft_id,),
        )
        await storage.conn.commit()

        for _ in range(50):
            async with storage.conn.execute(
                "SELECT COUNT(*) FROM meta_launch_drafts WHERE draft_id = ?",
                (draft.draft_id,),
            ) as cur:
                if (await cur.fetchone())[0] == 0:
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("periodic retention cleanup did not remove the expired raw prompt")
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_open_physically_purges_expired_prompt_without_a_list_call(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    draft, _ = await storage.stage_meta_launch_draft(
        session_key=SESSION_KEY,
        client_request_id=REQUEST_ID,
        meta_skill_name="meta-paper-write",
        launch_text=LAUNCH_TEXT,
    )
    await storage.conn.execute(
        "UPDATE meta_launch_drafts SET expires_at = 1 WHERE draft_id = ?",
        (draft.draft_id,),
    )
    await storage.conn.commit()
    await storage.close()

    reopened = await SessionStorage.open(str(db_path))
    try:
        async with reopened.conn.execute(
            "SELECT COUNT(*) FROM meta_launch_drafts WHERE draft_id = ?",
            (draft.draft_id,),
        ) as cur:
            assert (await cur.fetchone())[0] == 0
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_provisional_agent_recovery_is_not_starved_by_existing_sessions(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        for index in range(20):
            key = f"agent:main:webchat:existing-{index:02d}"
            await storage.upsert_session(SessionNode(
                session_key=key,
                session_id=f"existing-session-{index:02d}",
                agent_id="main",
                created_at=100 + index,
                updated_at=100 + index,
            ))
            await storage.stage_meta_launch_draft(
                session_key=key,
                client_request_id=f"existing-request-{index:02d}",
                meta_skill_name="meta-paper-write",
                launch_text=f"/meta meta-paper-write -- existing {index}",
            )
        provisional_key = "agent:main:webchat:provisional-after-twenty"
        await storage.stage_meta_launch_draft(
            session_key=provisional_key,
            client_request_id="provisional-after-twenty",
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- recover the provisional chat",
        )

        recovered = await storage.list_meta_launch_drafts(
            agent_id="main",
            provisional_only=True,
        )

        assert [(draft.session_key, draft.client_request_id) for draft in recovered] == [
            (provisional_key, "provisional-after-twenty")
        ]
    finally:
        await storage.close()
