from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from opensquilla.session.manager import SessionManager
from opensquilla.session.model_router_state import MODEL_DECISION_KEY, build_model_router_context
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage, StaleEpochError


def proposal(message_id: str, version: int = 1, tier: str = "c1") -> dict:
    return {
        "schema_version": 1,
        "input_message_id": message_id,
        "state": {
            "schema_version": "model-tier-state.v1",
            "policy_version": "four-tier-intent-policy.v1",
            "tier": tier,
            "task_id": "task",
            "turn_count": version,
            "version": version,
            "task_start_input_message_id": message_id,
            "generation_start_input_message_id": None,
        },
        "decision": {"tier": tier, "intent": "new_task", "classification": "immutable"},
    }


def active_state(rows, node):
    next_input = TranscriptEntry(
        session_id=node.session_id,
        session_key=node.session_key,
        message_id="next",
        role="user",
        content="continue",
    )
    return build_model_router_context(
        [*rows, next_input],
        bound_user_message_id="next",
        exclude_last_user=True,
    )["router_model_state"]


async def first_admitted(manager, node):
    first = await manager.append_message(node.session_key, "user", "first")
    await manager.stage_model_router_decision(
        node.session_key, first.message_id, proposal(first.message_id)
    )
    admitted = await manager.admit_model_router_decision(
        node.session_key, first.message_id, expected_previous_version=None
    )
    assert admitted is not None
    return first, admitted


@pytest.mark.asyncio
async def test_rejected_stage_survives_restart_without_advancing_task(tmp_path: Path):
    async with SessionStorage(tmp_path / "routing.db") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:admission")
        first, admitted = await first_admitted(manager, node)
        rejected = await manager.append_message(node.session_key, "user", "redo")
        original = proposal(rejected.message_id, 2, "c2")
        staged = await manager.stage_model_router_decision(
            node.session_key, rejected.message_id, original
        )
        assert staged["status"] == "staged" and staged["state_committed"] is False
        assert staged["decision"] == original["decision"]
        assert staged["decided_at_ms"] > 0
        await storage.close()
        await storage.connect()
        rows = await manager.get_model_router_transcript(node.session_key)
        assert active_state(rows, node) == admitted["state"]
        assert rows[-1].turn_context[MODEL_DECISION_KEY] == staged
        # A duplicate request retains its first classification and timestamp.
        assert (
            await manager.stage_model_router_decision(
                node.session_key, rejected.message_id, proposal(rejected.message_id, 3, "c3")
            )
            == staged
        )
        assert rows[0].message_id == first.message_id


@pytest.mark.asyncio
async def test_admission_cas_rejects_competing_state_and_is_idempotent():
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:cas")
        await first_admitted(manager, node)
        entries = [
            await manager.append_message(node.session_key, "user", text)
            for text in ("first candidate", "competing candidate")
        ]
        for entry in entries:
            await manager.stage_model_router_decision(
                node.session_key, entry.message_id, proposal(entry.message_id, 2, "c2")
            )
        admitted = await manager.admit_model_router_decision(
            node.session_key, entries[0].message_id, expected_previous_version=1
        )
        assert admitted["state_committed"] is True
        assert admitted["state_version_before"] == 1
        assert admitted["status"] == "admitted"
        assert (
            await manager.admit_model_router_decision(
                node.session_key, entries[1].message_id, expected_previous_version=1
            )
            is None
        )
        assert (
            await manager.admit_model_router_decision(
                node.session_key, entries[0].message_id, expected_previous_version=1
            )
            == admitted
        )
        with pytest.raises(StaleEpochError):
            await manager.admit_model_router_decision(
                node.session_key,
                entries[0].message_id,
                expected_previous_version=1,
                expected_session_id="replaced",
            )
        with pytest.raises(StaleEpochError):
            await manager.admit_model_router_decision(
                node.session_key,
                entries[0].message_id,
                expected_previous_version=1,
                expected_session_id=node.session_id,
                expected_session_epoch=1,
            )
        rows = await manager.get_model_router_transcript(node.session_key)
        assert active_state(rows, node) == admitted["state"]


@pytest.mark.asyncio
async def test_quality_cas_ignores_staged_candidates_and_never_upgrades_them():
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:quality-admission")
        first, admitted = await first_admitted(manager, node)
        rejected = await manager.append_message(node.session_key, "user", "candidate")
        staged = await manager.stage_model_router_decision(
            node.session_key, rejected.message_id, proposal(rejected.message_id, 2, "c2")
        )
        args = {
            "expected_session_id": node.session_id,
            "expected_session_epoch": node.epoch,
            "expected_version": 1,
            "target_tier": "c2",
            "reason": "no_progress",
            "scope_id": "quality",
            "execution_update": {"tier": "c2", "model": "model", "provider": "provider"},
        }
        upgraded = await manager.advance_model_router_quality_retry(
            node.session_key, first.message_id, **args
        )
        assert upgraded["state"]["version"] == 2
        assert upgraded["decision"] == admitted["decision"]
        assert (
            await manager.admit_model_router_decision(
                node.session_key, first.message_id, expected_previous_version=None
            )
            == upgraded
        )
        assert (
            await manager.admit_model_router_decision(
                node.session_key, rejected.message_id, expected_previous_version=1
            )
            is None
        )
        staged_args = deepcopy(args)
        staged_args.update(expected_version=2, target_tier="c3", execution_update={"tier": "c3"})
        assert (
            await manager.advance_model_router_quality_retry(
                node.session_key, rejected.message_id, **staged_args
            )
            is None
        )
        rows = await manager.get_model_router_transcript(node.session_key)
        assert rows[-1].turn_context[MODEL_DECISION_KEY] == staged


@pytest.mark.asyncio
async def test_admission_follows_accepted_row_into_compaction_archive():
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:archived-admission")
        accepted = await manager.append_message(node.session_key, "user", "request")
        staged = await manager.stage_model_router_decision(
            node.session_key, accepted.message_id, proposal(accepted.message_id)
        )
        rows = await storage.get_transcript(node.session_id)
        await storage.rewrite_compacted_session(
            node=node,
            summary=None,
            entries=[],
            archived_entries=rows,
            expected_session_id=node.session_id,
            expected_session_epoch=0,
        )
        admitted = await manager.admit_model_router_decision(
            node.session_key, accepted.message_id, expected_previous_version=None
        )
        assert admitted["decided_at_ms"] == staged["decided_at_ms"]
        assert admitted["admitted_at_ms"] >= admitted["decided_at_ms"]
        rows = await manager.get_model_router_transcript(node.session_key)
        assert rows[0].turn_context[MODEL_DECISION_KEY] == admitted
        assert active_state(rows, node) == admitted["state"]
