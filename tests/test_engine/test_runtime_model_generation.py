from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from opensquilla.engine.agent import Agent
from opensquilla.engine.runtime import RouterHistoryReplayRequest, TurnRunner
from opensquilla.engine.types import AgentConfig
from opensquilla.gateway.config import GatewayConfig
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionSummary
from opensquilla.session.storage import SessionStorage


def envelope(mid: str, *, generation: str | None = None, admitted: bool = True) -> dict:
    return {
        "state_committed": admitted,
        "status": "admitted" if admitted else "staged",
        "input_message_id": mid,
        "state": {
            "tier": "c1",
            "version": 1,
            "task_id": mid,
            "turn_count": 1,
            "task_start_input_message_id": mid,
            "generation_start_input_message_id": generation,
        },
    }


@pytest.mark.asyncio
async def test_explicit_generation_uses_ephemeral_window_and_preserves_timeline(monkeypatch):
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:generation")
        old = await manager.append_message(node.session_key, "user", "OLD SECRET " * 500)
        reset = await manager.append_message(node.session_key, "user", "new generation " * 500)
        await manager.update_message_turn_context(
            node.session_key,
            reset.message_id,
            {
                "router_model_decision": envelope(reset.message_id, generation=reset.message_id),
            },
        )
        await manager.append_message(node.session_key, "assistant", "generation answer " * 500)
        current = await manager.append_message(node.session_key, "user", "continue now")
        await storage.save_summary(
            SessionSummary(
                session_id=node.session_id,
                session_key=node.session_key,
                summary_text="OLD CHECKPOINT SECRET",
            )
        )
        before = await storage.get_canonical_transcript(node.session_id)
        runner = TurnRunner(
            provider_selector=MagicMock(), session_manager=manager, config=GatewayConfig()
        )
        monkeypatch.setattr(
            runner,
            "_load_compaction_context_records",
            AsyncMock(
                side_effect=AssertionError("generation must not load mixed checkpoints"),
            ),
        )

        def admitted(summary, rows):
            return len(summary) + sum(len(str(row.get("content") or "")) for row in rows) < 700

        window = await runner._maybe_preflight_compact(
            node.session_key,
            200,
            history_capacity_tokens=200,
            history_has_persisted_user=True,
            bound_user_message_id=current.message_id,
            expected_session_id=node.session_id,
            expected_session_epoch=0,
            consumer_admission=admitted,
        )
        assert window is not None and window.reason == "model_generation_context"
        assert all("OLD SECRET" not in row.content for row in window.kept_entries)
        queued = await manager.append_message(node.session_key, "user", "FUTURE SECRET")
        agent = Agent(
            provider=MagicMock(),
            config=AgentConfig(
                metadata={
                    "router_tier_policy": "model",
                    "routing_applied": True,
                }
            ),
        )
        summary = await runner._load_history(
            agent,
            node.session_key,
            prepared_window=window,
            bound_user_message_id=current.message_id,
            expected_session_id=node.session_id,
            expected_session_epoch=0,
        )
        assert "OLD CHECKPOINT" not in (summary or "")
        assert agent.config.metadata["router_model_prepared_window_admitted"] is True
        after = await storage.get_canonical_transcript(node.session_id)
        assert [row.model_dump() for row in before] == [row.model_dump() for row in after[:-1]]
        assert after[-1].message_id == queued.message_id
        assert all("FUTURE SECRET" not in str(message.content) for message in agent._history)
        assert (await manager.get_summaries(node.session_key))[
            0
        ].summary_text == "OLD CHECKPOINT SECRET"
        assert old.message_id in {
            row.message_id for row in await manager.get_transcript(node.session_key)
        }


@pytest.mark.asyncio
async def test_staged_explicit_reset_scopes_capacity_without_changing_active_state():
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:staged-generation")
        old = await manager.append_message(node.session_key, "user", "old " * 2000)
        await manager.update_message_turn_context(
            node.session_key,
            old.message_id,
            {
                "router_model_decision": envelope(old.message_id),
            },
        )
        current = await manager.append_message(node.session_key, "user", "new task")
        await manager.update_message_turn_context(
            node.session_key,
            current.message_id,
            {
                "router_model_decision": envelope(
                    current.message_id, generation=current.message_id, admitted=False
                ),
            },
        )
        runner = TurnRunner(
            provider_selector=MagicMock(), session_manager=manager, config=GatewayConfig()
        )
        measured = await runner._router_history_capacity_for_request(
            node.session_key,
            RouterHistoryReplayRequest(
                exclude_last_user=True,
                bound_user_message_id=current.message_id,
                expected_session_id=node.session_id,
                expected_session_epoch=0,
            ),
            max_history_turns=0,
            preserve_image_attachments=False,
        )
        assert measured["history_capacity_estimated_tokens"] == 0
        assert measured["history_capacity_estimate_complete"] is True
        context = await runner._router_previous_assistant_context(
            node.session_key,
            bound_user_message_id=current.message_id,
            exclude_last_user=True,
        )
        assert context["router_model_state"]["task_id"] == old.message_id


def test_outcome_and_blank_latest_assistant_match_frozen_projection():
    from opensquilla.session.model_router_state import build_model_router_context

    rows = [
        SimpleNamespace(role="user", message_id="a", content="earlier", turn_context={}),
        SimpleNamespace(role="assistant", message_id="b", content="old answer", turn_context={}),
        SimpleNamespace(
            role="assistant",
            message_id="c",
            content='{"text":"","artifacts":[]}',
            turn_context={"execution_status": "failed"},
        ),
        SimpleNamespace(role="user", message_id="d", content="now", turn_context={}),
    ]
    result = build_model_router_context(
        rows, bound_user_message_id="d", exclude_last_user=True
    )["router_model_input"]
    assert result["previous_answer"] == ""
    assert result["previous_outcome"] == "failure"
    rows[2].turn_context = {}
    result = build_model_router_context(
        rows, bound_user_message_id="d", exclude_last_user=True
    )["router_model_input"]
    assert result["previous_outcome"] == "unknown"


def test_features_use_active_slice_until_compacted_task_anchor_requires_canonical():
    from opensquilla.session.model_router_state import build_model_router_context

    old = SimpleNamespace(
        role="user", message_id="old", content="archived prior task", turn_context={}
    )
    anchor = SimpleNamespace(
        role="user",
        message_id="anchor",
        content="active task",
        turn_context={"router_model_decision": envelope("anchor")},
    )
    current = SimpleNamespace(
        role="user", message_id="current", content="continue", turn_context={}
    )
    rows = [old, anchor, current]
    options = dict(bound_user_message_id="current", exclude_last_user=True)
    active = build_model_router_context(rows, feature_entries=[anchor, current], **options)
    assert active["router_model_input"]["history_user"] == ["active task"]
    assert active["router_model_history_user_chars"] == len("active task")
    assert active["router_model_control_context"]["context_tokens_est"] > 0
    recovered = build_model_router_context(rows, feature_entries=[current], **options)
    assert recovered["router_model_input"]["history_user"] == ["archived prior task", "active task"]
    assert recovered["router_model_control_context"]["context_tokens_est"] == 0
    with pytest.raises(RuntimeError, match="feature boundary"):
        build_model_router_context(
            rows, feature_entries=[current], canonical_complete=False, **options
        )
    anchor.turn_context = {}
    no_state = build_model_router_context(rows, feature_entries=[current], **options)
    assert no_state["router_model_input"]["history_user"] == []

@pytest.fixture
async def image_generation(monkeypatch):
    import json

    from opensquilla.engine.steps import squilla_router
    from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig

    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:generation-image")
        image = {"type": "image/png", "data": "aGVsbG8="}
        await manager.append_message(
            node.session_key, "user", json.dumps({"text": "OLD IMAGE TASK", "attachments": [image]})
        )
        await manager.append_message(node.session_key, "assistant", "OLD IMAGE ANSWER")
        config = GatewayConfig(
            llm={
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-flash",
                "api_key": "synthetic-offline",
                "thinking": "off",
            }
        )
        selector = ModelSelector(
            SelectorConfig(
                primary=ProviderConfig(
                    provider="openrouter",
                    model=config.llm.model,
                    api_key="synthetic-offline",
                    base_url="https://openrouter.ai/api/v1",
                )
            )
        )
        runner = TurnRunner(provider_selector=selector, session_manager=manager, config=config)
        owner = dict(expected_session_id=node.session_id, expected_session_epoch=0)
        env = SimpleNamespace(
            manager=manager, storage=storage, node=node, runner=runner, config=config, owner=owner
        )

        async def route(text, tier, *, new_task=False, current_image=False):
            attachments = [image] if current_image else []
            content = (
                json.dumps({"text": text, "attachments": attachments}) if attachments else text
            )
            entry = await manager.append_message(node.session_key, "user", content)
            if new_task:
                await manager.update_message_turn_context(
                    node.session_key, entry.message_id, {"router_model_control_event": "new_task"}
                )
            context = await runner._router_previous_assistant_context(
                node.session_key,
                bound_user_message_id=entry.message_id,
                exclude_last_user=True,
                include_capacity=True,
                **owner,
            )
            env.context, env.entry = context, entry
            classifier = SimpleNamespace(
                predict=lambda _: SimpleNamespace(
                    intent_probabilities={"continue": 0.0, "redo": 0.0, "new_task": 1.0},
                    tier_probabilities={f"c{i}": float(tier == f"c{i}") for i in range(4)},
                )
            )
            monkeypatch.setattr(squilla_router, "_get_strategy", lambda _: classifier)
            return await runner._run_pipeline(
                text,
                node.session_key,
                selector.resolve(),
                selector,
                [],
                "system",
                attachments,
                model_router_context=context,
                history_has_recent_image=context.get("history_has_recent_image", False),
                bound_user_message_id=entry.message_id,
                **owner,
                router_history_replay_request=RouterHistoryReplayRequest(
                    exclude_last_user=True,
                    bound_user_message_id=entry.message_id,
                    **owner,
                ),
            )

        env.route = route
        yield env


@pytest.mark.asyncio
@pytest.mark.parametrize("followup", [False, True])
async def test_explicit_new_task_drops_prior_generation_image_requirement(
    image_generation, followup
):
    e = image_generation
    turn, _ = await e.route("Write a sorting function", "c0" if followup else "c1", new_task=True)
    assert turn.metadata["router_model_decision"]["decision"]["context_reset"] is True
    assert not e.context.get("history_has_recent_image", False)
    assert e.context["history_capacity_estimated_tokens"] == 0
    if followup:
        await e.manager.append_message(e.node.session_key, "assistant", "NEW TEXT ANSWER")
        turn, _ = await e.route("Explain the code complexity", "c1")
    assert turn.model == "deepseek/deepseek-v4-flash"
    assert not turn.metadata["image_context_has_images"]
    assert not e.context.get("history_has_recent_image", False)
    agent = Agent(provider=MagicMock(), config=AgentConfig())
    summary = await e.runner._load_history(
        agent, e.node.session_key, bound_user_message_id=e.entry.message_id, **e.owner
    )
    expected = (
        [("user", "Write a sorting function"), ("assistant", "NEW TEXT ANSWER")] if followup else []
    )
    assert [(message.role, message.content) for message in agent._history] == expected
    assert summary is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "image_source", ["current", "generation_history", "archived_generation_history"]
)
async def test_model_generation_still_requires_vision_for_actual_images(
    image_generation, image_source
):
    from opensquilla.engine.steps.model_router import ModelRouteError

    e = image_generation
    if image_source != "current":
        await e.route("Inspect this new image", "c0", new_task=True, current_image=True)
        await e.manager.append_message(e.node.session_key, "assistant", "NEW IMAGE ANSWER")
        if image_source == "archived_generation_history":
            rows = await e.storage.get_transcript(e.node.session_id)
            node = await e.manager.get_session(e.node.session_key)
            node.compaction_count += 1
            await e.storage.rewrite_compacted_session(
                node=node,
                summary=SessionSummary(
                    session_id=node.session_id,
                    session_key=node.session_key,
                    summary_text="old checkpoint",
                    compaction_id="image-generation-test",
                    removed_count=len(rows) - 1,
                    kept_count=1,
                    covered_through_id=rows[-2].id,
                ),
                entries=rows[-1:],
                archived_entries=rows[:-1],
                expected_source_entries=rows,
                **e.owner,
            )
            assert (
                await e.storage.get_canonical_transcript_coverage(node.session_id)
            ).canonical_complete
    with pytest.raises(ModelRouteError, match="no proven image support"):
        await e.route(
            "Explain this",
            "c1",
            new_task=image_source == "current",
            current_image=image_source == "current",
        )
    assert e.context.get("history_has_recent_image", False) == (image_source != "current")


@pytest.mark.asyncio
async def test_observed_new_task_keeps_existing_image_history(image_generation):
    e = image_generation
    e.config.squilla_router.rollout_phase = "observe"
    entry = await e.manager.append_message(e.node.session_key, "user", "Observe a new task")
    await e.manager.update_message_turn_context(
        e.node.session_key, entry.message_id, {"router_model_control_event": "new_task"}
    )
    context = await e.runner._router_previous_assistant_context(
        e.node.session_key,
        bound_user_message_id=entry.message_id,
        exclude_last_user=True,
        **e.owner,
    )
    assert context["history_has_recent_image"] is True
