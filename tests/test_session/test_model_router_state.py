from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig
from opensquilla.session.manager import SessionManager
from opensquilla.session.model_router_state import (
    MODEL_DECISION_KEY,
    build_model_router_context,
    model_generation_transcript,
)
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionNode,
    TranscriptEntry,
)
from opensquilla.session.storage import SessionStorage, StaleEpochError


def record(
    input_id: str,
    tier: str = "c2",
    *,
    task_id: str = "task-a",
    count: int = 1,
    task_start: str | None = None,
    generation_start: str | None = None,
    version: int = 1,
) -> dict:
    return {
        "schema_version": 1,
        "status": "admitted",
        "state_committed": True,
        "input_message_id": input_id,
        "state": {
            "schema_version": "model-tier-state.v1",
            "policy_version": "four-tier-intent-policy.v1",
            "tier": tier,
            "task_id": task_id,
            "turn_count": count,
            "version": version,
            "task_start_input_message_id": task_start or input_id,
            "generation_start_input_message_id": generation_start,
        },
        "decision": {"tier": tier, "intent": "new_task", "audit": "test"},
    }


async def admit(manager, session_key, message_id, decision, **kwargs):
    await manager.stage_model_router_decision(session_key, message_id, decision, **kwargs)
    result = await manager.admit_model_router_decision(
        session_key,
        message_id,
        expected_previous_version=decision["state"]["version"] - 1 or None,
        **kwargs,
    )
    assert result is not None
    return result


def user(mid: str, text: str, decision: dict | None = None, *, turn_id: str | None = None):
    context = {"turn_id": turn_id or mid}
    if decision:
        context[MODEL_DECISION_KEY] = decision
    return TranscriptEntry(
        session_id="sid",
        session_key="agent:main:router",
        role="user",
        message_id=mid,
        content=text,
        turn_context=context,
    )


def context(rows, bound: str):
    return build_model_router_context(
        rows, bound_user_message_id=bound, exclude_last_user=True
    )


def test_state_survives_long_gap_and_missing_assistant_without_future_leak():
    old = user("old", "task anchor " + "x" * 8000, record("old"))
    old.created_at = 1
    current = user("now", "continue")
    future = user("future", "future secret", record("future", "c3"))
    result = context([old, current, future], "now")
    assert result["router_model_state"] == old.turn_context[MODEL_DECISION_KEY]["state"]
    assert result["router_model_input"]["task_anchor"] == old.content
    assert result["router_model_input"]["history_user"] == [old.content]
    assert result["router_model_input"]["previous_outcome"] == "unknown"


def test_cross_task_history_full_usage_and_queued_previous_reply():
    first = user("a", "old task", record("a"))
    second = user("b", "new task " + "z" * 9000, record("b", "c1", task_id="task-b"))
    current = user("c", "continue")
    future = user("d", "future")
    assistant = TranscriptEntry(
        session_id="sid",
        session_key=first.session_key,
        role="assistant",
        content="answer " + "r" * 15000,
        turn_context={"turn_id": "b"},
        turn_usage={
            "model": "model-actual",
            "routed_tier": "c1",
            "output_tokens": 3000,
            "reasoning_tokens": 1200,
            "input_tokens": 2400,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
        },
    )
    result = context([first, second, current, future, assistant], "c")["router_model_input"]
    assert result["history_user"] == [first.content, second.content]
    assert result["task_anchor"] == second.content
    assert result["previous_answer"] == assistant.content
    assert result["previous_usage"] == assistant.turn_usage


def test_fork_aliases_restore_task_anchor_and_explicit_generation_boundary():
    old = user("fork-old", "old", record("old"))
    new = user("fork-new", "new", record("new", task_id="new", generation_start="new"))
    follow = user(
        "fork-follow",
        "continue",
        record("follow", task_id="new", count=2, task_start="new", generation_start="new"),
    )
    current = user("current", "again")
    rows = [old, new, follow, current]
    result = context(rows, "current")
    assert result["router_model_input"]["task_anchor"] == "new"
    assert result["router_model_input"]["history_user"] == ["old", "new", "continue"]
    assert model_generation_transcript(rows, "current") == [new, follow, current]


def test_replay_decision_is_separate_from_prior_state_and_future_assistant():
    prior = user("prior", "first", record("prior", "c1"))
    current = user("current", "redo", record("current", "c2", count=2, task_start="prior"))
    assistant = TranscriptEntry(
        session_id="sid",
        session_key=prior.session_key,
        role="assistant",
        content="current answer",
        turn_context={"turn_id": "current"},
    )
    result = context([prior, current, assistant], "current")
    assert result["router_model_state"]["tier"] == "c1"
    assert result["router_model_decision"] == current.turn_context[MODEL_DECISION_KEY]
    assert result["router_model_input"]["previous_answer"] == ""


def test_missing_bound_input_fails_closed():
    with pytest.raises(RuntimeError, match="absent"):
        context([user("a", "not current")], "missing")


def test_feature_usage_requires_complete_counters_and_strips_execution_metadata():
    from opensquilla.session.model_router_state import canonical_previous_usage

    counters = {
        "input_tokens": 1,
        "output_tokens": 2,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert canonical_previous_usage(
        {
            **counters,
            "model": "actual",
            "route_id": "route",
            "execution_status": "failed",
            "retry_count": 2,
        }
    ) == {
        **counters,
        "model": "actual",
    }
    assert canonical_previous_usage({"output_tokens": 2}) == {}
    assert canonical_previous_usage({**counters, "cached_tokens": True}) == {}


def test_blank_attachment_inputs_do_not_consume_feature_history_slots():
    rows = [user(str(i), str(i) * (i + 1)) for i in range(6)]
    rows.append(user("empty", '{"text":"  ","attachments":[{"mime":"image/png"}]}'))
    rows.append(user("current", "now"))
    result = context(rows, "current")
    assert result["router_model_input"]["history_user"] == ["222", "3333", "44444", "555555"]
    assert result["router_model_history_user_chars"] == 21


@pytest.mark.asyncio
async def test_stage_survives_restart_is_idempotent_and_preserves_lifecycle_context(
    tmp_path: Path,
):
    path = tmp_path / "sessions.db"
    storage = SessionStorage(path)
    await storage.connect()
    node = SessionNode(session_key="agent:main:router", session_id="sid")
    await storage.upsert_session(node)
    entry = user("accepted", "request")
    entry.turn_context["router_model_control_event"] = "new_task"
    await storage.append_transcript_entry(entry, expected_epoch=0)
    first = {
        **record("accepted"),
        "status": "staged",
        "state_committed": False,
        "decided_at_ms": 12345,
    }
    assert (
        await storage.stage_model_router_decision(
            node.session_key,
            entry.message_id,
            first,
            expected_session_id=node.session_id,
            expected_epoch=0,
        )
        == first
    )
    assert (
        await storage.stage_model_router_decision(
            node.session_key,
            entry.message_id,
            record("accepted", "c3"),
            expected_session_id=node.session_id,
            expected_epoch=0,
        )
        == first
    )
    await storage.update_transcript_turn_context(
        node.session_key,
        entry.message_id,
        {"turn_id": "promoted", "disposition": "promoted", "router_model_control_event": "redo"},
    )
    await storage.close()
    await storage.connect()
    try:
        rows = await storage.get_canonical_transcript(node.session_id)
        assert rows[0].turn_context == {
            "turn_id": "promoted",
            "disposition": "promoted",
            "router_model_control_event": "new_task",
            MODEL_DECISION_KEY: first,
        }
        with pytest.raises(StaleEpochError):
            await storage.stage_model_router_decision(
                node.session_key,
                entry.message_id,
                first,
                expected_session_id="replaced-session",
                expected_epoch=0,
            )
        with pytest.raises(RuntimeError, match="no accepted"):
            await storage.stage_model_router_decision(
                node.session_key,
                "absent",
                first,
                expected_session_id=node.session_id,
                expected_epoch=0,
            )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_runtime_reads_canonical_model_state_with_complete_usage():
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        node = await manager.create("agent:main:router")
        accepted = await manager.append_message(node.session_key, "user", "earlier")
        await admit(manager, node.session_key, accepted.message_id, record(accepted.message_id))
        await manager.append_message(
            node.session_key,
            "assistant",
            "answer",
            turn_usage={
                "output_tokens": 100,
                "reasoning_tokens": 90,
                "input_tokens": 200,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "model": "actual-model",
            },
        )
        current = await manager.append_message(node.session_key, "user", "continue")
        runner = TurnRunner(
            provider_selector=MagicMock(), session_manager=manager, config=GatewayConfig()
        )
        result = await runner._router_previous_assistant_context(
            node.session_key,
            exclude_last_user=True,
            bound_user_message_id=current.message_id,
            expected_session_id=node.session_id,
            expected_session_epoch=0,
        )
        assert result["router_model_state"]["tier"] == "c2"
        assert result["router_model_input"]["previous_usage"]["reasoning_tokens"] == 90
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_pipeline_commits_state_before_capacity_error_and_observe_does_not(
    monkeypatch,
    tmp_path: Path,
):
    from opensquilla.engine import pipeline, steps

    storage = SessionStorage(tmp_path / "sessions.db")
    await storage.connect()
    manager = SessionManager(storage)
    try:
        node = await manager.create("agent:main:model-commit")
        entry = await manager.append_message(node.session_key, "user", "request")
        envelope = record(entry.message_id)
        apply = True

        async def fake_pipeline(turn, _steps, **kwargs):
            turn.metadata.update(
                {
                    "routing_applied": apply,
                    "router_model_decision": envelope,
                }
            )
            return turn

        async def fail_capacity(turn, **kwargs):
            raise ValueError("physical capacity denied")

        monkeypatch.setattr(pipeline, "run_pipeline", fake_pipeline)
        monkeypatch.setattr(steps, "finalize_squilla_router_capacity", fail_capacity)
        runner = TurnRunner(
            provider_selector=MagicMock(), session_manager=manager, config=GatewayConfig()
        )
        apply = False
        with pytest.raises(ValueError, match="physical capacity"):
            await runner._run_pipeline(
                "request",
                node.session_key,
                None,
                None,
                [],
                "",
                [],
                bound_user_message_id=entry.message_id,
            )
        before = await storage.get_canonical_transcript(node.session_id)
        assert MODEL_DECISION_KEY not in (before[0].turn_context or {})
        apply = True
        with pytest.raises(ValueError, match="physical capacity"):
            await runner._run_pipeline(
                "request",
                node.session_key,
                None,
                None,
                [],
                "",
                [],
                bound_user_message_id=entry.message_id,
            )
        after = await storage.get_canonical_transcript(node.session_id)
        persisted = after[0].turn_context[MODEL_DECISION_KEY]
        assert persisted["state"] == envelope["state"]
        assert persisted["status"] == "staged"
        assert persisted["state_committed"] is False
        next_input = user("next", "continue")
        assert context([*after, next_input], "next")["router_model_state"] is None
        assert persisted["decision"] == envelope["decision"]
        assert persisted["decided_at_ms"] > 0
        with pytest.raises(RuntimeError, match="durable accepted"):
            await runner._run_pipeline("request", node.session_key, None, None, [], "", [])
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_canonical_compaction_and_prefix_fork_keep_only_owned_state():
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        node = await manager.create("agent:main:canonical-state")
        first = await manager.append_message(node.session_key, "user", "anchor")
        first_record = record(first.message_id)
        first_record = await admit(manager, node.session_key, first.message_id, first_record)
        current = await manager.append_message(node.session_key, "user", "current")
        rows = await storage.get_transcript(node.session_id)
        await storage.rewrite_compacted_session(
            node=node,
            summary=None,
            entries=[rows[1]],
            archived_entries=[rows[0]],
            expected_session_id=node.session_id,
            expected_session_epoch=0,
        )
        canonical = await manager.get_model_router_transcript(node.session_key)
        assert context(canonical, current.message_id)["router_model_state"] == first_record["state"]
        assert (
            await admit(
                manager,
                node.session_key,
                first.message_id,
                record(first.message_id, "c3"),
            )
            == first_record
        )
        await admit(
            manager,
            node.session_key,
            current.message_id,
            record(current.message_id, "c3", version=2),
        )
        fork = await manager.prepare_prefix_branch(
            node.session_key,
            "agent:main:canonical-fork",
            fork_before_message_id=current.message_id,
        )
        child_rows = list(fork.initial_transcript_entries)
        child_current = user("child-new", "continue")
        recovered = context([*child_rows, child_current], child_current.message_id)
        assert recovered["router_model_state"]["tier"] == "c2"
        assert recovered["router_model_input"]["task_anchor"] == "anchor"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_previous_outcome_uses_terminal_ledger_when_reply_missing():
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        node = await manager.create("agent:main:failed-state")
        first = await manager.append_message(node.session_key, "user", "request")
        await manager.update_message_turn_context(
            node.session_key, first.message_id, {"turn_id": "failed-turn"}
        )
        await admit(manager, node.session_key, first.message_id, record(first.message_id))
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="failed-turn",
                session_key=node.session_key,
                status=AgentTaskStatus.FAILED,
                details={
                    "turn_id": "failed-turn",
                    "turn_outcome": {"kind": "failed", "reason": "provider"},
                },
            )
        )
        current = await manager.append_message(node.session_key, "user", "retry")
        rows = await manager.get_model_router_transcript(node.session_key)
        assert (
            context(rows, current.message_id)["router_model_input"]["previous_outcome"] == "failure"
        )
    finally:
        await storage.close()


def test_source_route_features_and_explicit_reset_keep_cross_task_input():
    import time

    prior = user("a", "  earlier text  ", record("a", "c1"))
    prior.turn_context[MODEL_DECISION_KEY].update(
        {
            "decided_at_ms": int(time.time() * 1000),
            "updated_at_ms": int(time.time() * 1000) + 1,
            "decision": {
                "tier_audit": {"probabilities": {"c0": 0.1, "c1": 0.6, "c2": 0.2, "c3": 0.1}}
            },
        }
    )
    current = user("b", "new task")
    current.turn_context["router_model_control_event"] = "new_task"
    result = context([prior, current], "b")
    assert result["router_model_input"]["history_user"] == ["earlier text"]
    assert result["router_model_input"]["route_history"] == [
        {"tier_id": "C1", "difficulty": pytest.approx(1.3), "margin": pytest.approx(0.4)},
    ]
    control = result["router_model_control_context"]
    assert control["context_tokens_est"] == 0


def test_failed_latest_route_does_not_pair_an_older_answer_and_attachments_are_normalized():
    from opensquilla.session.model_router_state import normalized_router_attachments

    old = user("old", "old", record("old"))
    answer = TranscriptEntry(
        session_id="sid",
        session_key=old.session_key,
        role="assistant",
        content="old answer",
        turn_context={"turn_id": "old"},
    )
    failed = user("failed", "failed", record("failed"))
    now = user("now", "continue")
    assert context([old, answer, failed, now], "now")["router_model_input"]["previous_answer"] == ""
    assert normalized_router_attachments(
        [{"mime": "image/png", "name": "private.png", "data": "private bytes", "token_count": 123}]
    ) == [
        {
            "type": "image",
            "mime_type": None,
            "media_type": None,
            "parse_status": None,
            "status": None,
            "summary": None,
            "truncated": None,
            "token_count": 123,
        }
    ]
    assert normalized_router_attachments([{"mime_type": "application/pdf"}, {}]) == []


@pytest.mark.asyncio
async def test_quality_retry_cas_retains_prediction_and_restart_uses_final_tier(tmp_path: Path):
    storage = SessionStorage(tmp_path / "quality.db")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        node = await manager.create("agent:main:quality")
        accepted = await manager.append_message(node.session_key, "user", "request")
        original = record(accepted.message_id, "c1")
        await admit(manager, node.session_key, accepted.message_id, original)
        args = dict(
            expected_session_id=node.session_id,
            expected_session_epoch=0,
            expected_version=1,
            target_tier="c2",
            reason="no_progress",
            scope_id="scope",
            execution_update={
                "tier": "c2",
                "model": "retry-model",
                "provider": "test",
                "thinking_mode": "T2",
                "prompt_policy": "P1",
                "thinking_level": "medium",
            },
        )
        advanced = await manager.advance_model_router_quality_retry(
            node.session_key,
            accepted.message_id,
            **args,
        )
        assert advanced["decision"] == original["decision"]
        assert advanced["state"] == {**original["state"], "tier": "c2", "version": 2}
        assert advanced["quality_retry"]["used"] is True
        assert (
            await manager.advance_model_router_quality_retry(
                node.session_key,
                accepted.message_id,
                **args,
            )
            is None
        )
        await storage.close()
        await storage.connect()
        current = await manager.append_message(node.session_key, "user", "continue")
        rows = await manager.get_model_router_transcript(node.session_key)
        recovered = context(rows, current.message_id)
        assert recovered["router_model_state"]["tier"] == "c2"
        with pytest.raises(StaleEpochError):
            await manager.advance_model_router_quality_retry(
                node.session_key,
                accepted.message_id,
                **{**args, "expected_session_id": "wrong"},
            )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_runtime_model_input_matches_source_projection(monkeypatch):
    from types import SimpleNamespace

    from opensquilla.engine import pipeline, steps

    captured = {}

    async def capture(turn, _steps, **kwargs):
        captured.update(turn.metadata)
        return turn

    async def stop(turn, **kwargs):
        raise ValueError("capture boundary")

    monkeypatch.setattr(pipeline, "run_pipeline", capture)
    monkeypatch.setattr(steps, "finalize_squilla_router_capacity", stop)
    runner = TurnRunner(provider_selector=MagicMock(), config=GatewayConfig())
    partial = {
        "task_anchor": "a" * 8000,
        "history_user": ["12345"],
        "previous_answer": "abcdef",
        "route_history": [{"tier_id": "C1"}],
        "previous_usage": {"reasoning_tokens": 99},
    }
    with pytest.raises(ValueError, match="capture boundary"):
        await runner._run_pipeline(
            "physical wrapper",
            "session",
            None,
            None,
            [{"name": "z"}, SimpleNamespace(name="a"), {"name": "z"}],
            "",
            [],
            semantic_message="raw request",
            model_router_context={
                "router_model_input": partial,
                "router_model_control_context": {"context_tokens_est": 17},
                "router_model_history_user_chars": 100,
            },
        )
    projected = captured["router_model_input"]
    assert projected["current_request"] == "raw request"
    assert projected["task_anchor"] == "a" * 8000
    assert projected["context"] == {"turn_index": 1, "context_tokens_est": 29}
    assert projected["tool_state"] == {"available_tools": ["a", "z"]}
    assert projected["previous_usage"] == {}
    assert captured["router_model_control_context"]["context_tokens_est"] == 20
