"""Scheduled turns retain their source through the real transcript writer."""

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.turn_runner.harness import _TurnRunnerTranscriptAppendAdapter
from opensquilla.engine.turn_runner.turn_finalizer_stage import (
    TurnFinalizerStage,
    TurnFinalizerStageInput,
)
from opensquilla.engine.types import DoneEvent
from opensquilla.scheduler.delivery import DeliveryChain
from opensquilla.scheduler.handlers import make_agent_run_handler
from opensquilla.scheduler.types import CronJob, SessionTarget
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionIntent
from opensquilla.session.storage import SessionStorage, StaleEpochError


class _NoopEffects:
    async def capture_turn(self, **_: Any) -> None:
        pass

    async def persist_error(self, **_: Any) -> None:
        pass

    async def rollup(self, **_: Any) -> None:
        pass


async def _finish(
    manager: SessionManager,
    session_key: str,
    *,
    run_kind: str,
    input_provenance: dict[str, Any] | None,
    expected_session_id: str,
    expected_session_epoch: int,
) -> None:
    runner = SimpleNamespace(
        _session_manager=manager,
        _append_session_message=manager.append_message,
    )
    effects = _NoopEffects()
    stage = TurnFinalizerStage(
        transcript_append=_TurnRunnerTranscriptAppendAdapter(runner),
        turn_memory_capture=effects,
        turn_error_persist=effects,
        session_totals=effects,
    )
    await stage.run(TurnFinalizerStageInput(
        final_text_parts=["Scheduled inventory: 12 items."],
        turn_segments=[],
        turn_artifacts=[],
        error_message=None,
        pending_error_event=None,
        done_event=None,
        runtime_message="Count the synthetic inventory.",
        input_mode="user",
        input_provenance=input_provenance,
        resolved_model="synthetic-model",
        agent_id="main",
        session_key=session_key,
        tool_context=None,
        run_kind=run_kind,
        heartbeat_ack_max_chars=300,
        no_memory_capture=True,
        expected_session_id=expected_session_id,
        expected_session_epoch=expected_session_epoch,
    ))


class _ScheduledRunner:
    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager

    async def run(
        self,
        message: str,
        session_key: str,
        *,
        run_kind: str,
        input_provenance: dict[str, Any],
        expected_session_id: str,
        expected_session_epoch: int,
        **_: Any,
    ):
        await _finish(
            self.manager,
            session_key,
            run_kind=run_kind,
            input_provenance=input_provenance,
            expected_session_id=expected_session_id,
            expected_session_epoch=expected_session_epoch,
        )
        yield DoneEvent(text_snapshot="Scheduled inventory: 12 items.")


@pytest.mark.parametrize("target", [SessionTarget.CURRENT, SessionTarget.ISOLATED])
async def test_cron_prompt_and_reply_sources_survive_reopen(tmp_path, target) -> None:
    database = str(tmp_path / "sessions.db")
    storage = SessionStorage(database)
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    origin = "agent:main:webchat:inventory"
    original = await manager.create(origin)
    await manager.append_message(origin, "user", "There are 12 items.")
    job = CronJob(
        id="inventory-check",
        name="Inventory check",
        handler_key="agent_run",
        payload={"kind": "agent_turn", "task": "Count the synthetic inventory."},
        session_target=target,
        session_key=origin if target == SessionTarget.CURRENT else "",
        origin_session_key=origin,
    )
    handler = make_agent_run_handler(
        DeliveryChain(),
        turn_runner_ref=lambda: _ScheduledRunner(manager),
        session_manager_ref=lambda: manager,
    )
    try:
        result = await handler(job)
        run_key = result.session_key
        entries = await manager.get_transcript(run_key)
        assert [entry.role for entry in entries[-2:]] == ["user", "assistant"]
        for entry in entries[-2:]:
            assert entry.provenance_kind == "cron"
            assert entry.provenance_source_tool == "cron:inventory-check"
            assert entry.provenance_source_session_key == run_key
        assert (await storage.get_session(origin)).session_id == original.session_id
        if target == SessionTarget.CURRENT:
            assert entries[0].content == "There are 12 items."
            assert entries[0].provenance_kind is None
        message_ids = [entry.message_id for entry in entries]
    finally:
        await storage.close()

    reopened = SessionStorage(database)
    await reopened.connect()
    try:
        history = await SessionManager(reopened).get_transcript(run_key)
        assert [entry.message_id for entry in history] == message_ids
        assert [entry.provenance_kind for entry in history[-2:]] == ["cron", "cron"]
        owner = await reopened.get_session(run_key)
        await _finish(
            SessionManager(reopened),
            run_key,
            run_kind="session_turn",
            input_provenance={"kind": "runtime_send"},
            expected_session_id=owner.session_id,
            expected_session_epoch=owner.epoch,
        )
        continued = await SessionManager(reopened).get_transcript(run_key)
        assert continued[-1].provenance_kind is None
    finally:
        await reopened.close()


async def test_cron_reply_source_does_not_bypass_session_owner(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    key = "agent:main:webchat:reset-inventory"
    try:
        owner = await manager.create(key)
        await manager.apply_intent(key, SessionIntent.RESET_SAME_KEY)
        with pytest.raises(StaleEpochError):
            await _finish(
                manager,
                key,
                run_kind="cron_turn",
                input_provenance={"kind": "cron_job", "job_id": "inventory-check"},
                expected_session_id=owner.session_id,
                expected_session_epoch=owner.epoch,
            )
        assert await manager.get_transcript(key) == []
    finally:
        await storage.close()
