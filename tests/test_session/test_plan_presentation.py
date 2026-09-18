"""Presentation mutations preserve immutable history and execution ownership."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opensquilla.persistence.plan_presentation import (
    PlanPresentationConflictError,
    PlanPresentationRequestConflictError,
)
from opensquilla.session.models import SessionNode
from opensquilla.session.plans import PlanValidationError, new_plan_revision
from opensquilla.session.storage import SessionStorage, StaleEpochError

KEY = "agent:main:webchat:presentation-test"


async def seed(storage: SessionStorage):
    await storage.upsert_session(SessionNode(
        session_key=KEY, session_id="presentation-session", agent_id="main",
        created_at=100, updated_at=100,
    ))
    return await storage.create_plan_revision(new_plan_revision(
        source_session_key=KEY, source_session_id="presentation-session", source_epoch=0,
        title="Synthetic proposal", markdown="Keep the original plan.",
        steps=[{"title": "Inspect"}],
    ), expected_parent_revision_id=None)


async def change(storage: SessionStorage, revision_id: str, **overrides):
    return await storage.set_plan_presentation(KEY, revision_id, **{
        "dismissed": True, "expected_epoch": 0, "expected_presentation_revision": 0,
        "client_request_id": "presentation-request", **overrides,
    })


@pytest.mark.asyncio
async def test_presentation_survives_reopen_without_altering_plan_or_mode(tmp_path: Path):
    path = str(tmp_path / "presentation.sqlite")
    async with await SessionStorage.open(path) as storage:
        plan = await seed(storage)
        session = await storage.get_session(KEY)
        hidden, replayed = await change(storage, plan.revision_id)
        assert hidden == {"revisionId": plan.revision_id, "dismissed": True, "stateRevision": 1}
        assert not replayed
        assert await storage.get_plan_revision(plan.revision_id) == plan
        assert await storage.get_session(KEY) == session
        assert await storage.get_active_plan_run(KEY) is None
    async with await SessionStorage.open(path) as storage:
        assert await storage.get_plan_presentations(KEY) == [hidden]
        restored, replayed = await change(
            storage, plan.revision_id, dismissed=False,
            expected_presentation_revision=1, client_request_id="restore-request",
        )
        assert not replayed
        assert restored["dismissed"] is False
        assert restored["stateRevision"] == 2
        # Replay is the original receipt; a stale CAS must not create a second write.
        assert await change(storage, plan.revision_id) == (hidden, True)
        assert await storage.get_plan_presentations(KEY) == [restored]


@pytest.mark.asyncio
async def test_presentation_receipt_cannot_be_reused_for_different_intent(tmp_path: Path):
    storage = await SessionStorage.open(str(tmp_path / "receipts.sqlite"))
    try:
        plan = await seed(storage)
        await change(storage, plan.revision_id)
        with pytest.raises(PlanPresentationRequestConflictError):
            await change(storage, plan.revision_id, dismissed=False)
        with pytest.raises(PlanPresentationConflictError):
            await change(storage, plan.revision_id, client_request_id="stale-request")
        # Failed CAS did not leave a receipt that prevents retry at the new revision.
        state, replayed = await change(
            storage, plan.revision_id, expected_presentation_revision=1,
            client_request_id="stale-request", dismissed=False,
        )
        assert state["stateRevision"] == 2 and not replayed
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_presentation_epoch_check_precedes_receipt_replay(tmp_path: Path):
    storage = await SessionStorage.open(str(tmp_path / "epoch.sqlite"))
    try:
        plan = await seed(storage)
        await change(storage, plan.revision_id)
        await storage.conn.execute("UPDATE sessions SET epoch = 1 WHERE session_key = ?", (KEY,))
        await storage.conn.commit()
        with pytest.raises(StaleEpochError):
            await change(storage, plan.revision_id)
        assert await storage.get_plan_presentations(KEY) == []
        with pytest.raises(PlanValidationError):
            await change(storage, plan.revision_id, expected_epoch=1)
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_presentation_racing_writers_commit_once(tmp_path: Path):
    storage = await SessionStorage.open(str(tmp_path / "race.sqlite"))
    try:
        plan = await seed(storage)
        results = await asyncio.gather(
            change(storage, plan.revision_id, client_request_id="first"),
            change(storage, plan.revision_id, client_request_id="second", dismissed=False),
            return_exceptions=True,
        )
        assert sum(isinstance(value, PlanPresentationConflictError) for value in results) == 1
        assert len(await storage.get_plan_presentations(KEY)) == 1
        assert (await storage.get_plan_presentations(KEY))[0]["stateRevision"] == 1
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_historical_plan_visibility_and_foreign_session_isolation(tmp_path: Path):
    storage = await SessionStorage.open(str(tmp_path / "historical.sqlite"))
    try:
        first = await seed(storage)
        second = await storage.create_plan_revision(new_plan_revision(
            source_session_key=KEY, source_session_id="presentation-session", source_epoch=0,
            parent=first,
            title="Revised proposal", markdown="Preserve both revisions.",
            steps=[{"title": "Verify"}],
        ), expected_parent_revision_id=first.revision_id)
        hidden, _ = await change(storage, first.revision_id)
        assert (await storage.get_current_plan_revision(KEY)).revision_id == second.revision_id
        assert await storage.get_plan_presentations(KEY) == [hidden]
        foreign = "agent:main:webchat:foreign-presentation"
        await storage.upsert_session(SessionNode(
            session_key=foreign, session_id="foreign-session", agent_id="main",
            created_at=100, updated_at=100,
        ))
        with pytest.raises(PlanValidationError):
            await storage.set_plan_presentation(
                foreign, first.revision_id, dismissed=True, expected_epoch=0,
                expected_presentation_revision=0, client_request_id="foreign-request",
            )
        assert await storage.get_plan_presentations(foreign) == []
    finally:
        await storage.close()
