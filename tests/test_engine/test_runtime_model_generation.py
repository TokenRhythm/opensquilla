from __future__ import annotations

import asyncio
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
@pytest.mark.parametrize("summary_mode", [
    "unavailable", "disabled", "timeout", "cancelled", "unexpected-error",
])
async def test_explicit_generation_uses_ephemeral_window_and_preserves_timeline(
    monkeypatch, summary_mode,
):
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
            provider_selector=MagicMock(), session_manager=manager,
            config=GatewayConfig(compaction={"enabled": summary_mode != "disabled"}),
        )
        from opensquilla.session.compaction_lifecycle import CompactionTimeoutError

        failures = {
            "disabled": AssertionError("disabled compaction must not summarize"),
            "timeout": CompactionTimeoutError("summarizing"),
            "cancelled": asyncio.CancelledError(),
            "unexpected-error": RuntimeError("summary accounting failed"),
        }
        summarizer = None
        if summary_mode in failures:
            summarizer = AsyncMock(side_effect=failures[summary_mode])
            monkeypatch.setattr("opensquilla.session.compaction.compact_context", summarizer)
        monkeypatch.setattr(
            runner,
            "_load_compaction_context_records",
            AsyncMock(
                side_effect=AssertionError("generation must not load mixed checkpoints"),
            ),
        )

        def admitted(summary, rows):
            return len(summary) + sum(len(str(row.get("content") or "")) for row in rows) < 700

        prepare = runner._maybe_preflight_compact(
            node.session_key,
            200,
            history_capacity_tokens=200,
            history_has_persisted_user=True,
            bound_user_message_id=current.message_id,
            expected_session_id=node.session_id,
            expected_session_epoch=0,
            consumer_admission=admitted,
        )
        if summary_mode in {"cancelled", "unexpected-error"}:
            with pytest.raises(type(failures[summary_mode])):
                await prepare
            assert [row.model_dump() for row in before] == [
                row.model_dump() for row in await storage.get_canonical_transcript(node.session_id)
            ]
            return
        window = await prepare
        if summary_mode == "disabled":
            summarizer.assert_not_awaited()
        elif summary_mode == "timeout":
            summarizer.assert_awaited_once()
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


@pytest.mark.asyncio
async def test_new_default_generation_summary_preserves_facts_and_tool_pairs_on_wire(
    tmp_path, monkeypatch,
):
    """The offline responder can only repeat facts it actually receives."""
    import functools
    import json

    import httpx

    from opensquilla.engine.routing.model_policy import ModelPolicyState
    from opensquilla.engine.steps import squilla_router
    from opensquilla.gateway.config import ModelOverrideConfig, SquillaRouterConfig
    from opensquilla.provider import model_catalog
    from opensquilla.provider.model_catalog import ModelCatalog
    from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
    from opensquilla.provider.types import ModelInfo
    from opensquilla.token_estimation import estimate_tokens
    from opensquilla.tools.registry import ToolRegistry
    from opensquilla.tools.types import ToolContext
    from scripts import live_reasoning_replay_e2e as harness

    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-openrouter")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    model, window = "qwen/qwen-2.5-coder-32b-instruct", 32768
    endpoint = harness.registry_endpoint("openrouter")
    config = harness._config(tmp_path, "openrouter", model, endpoint)
    config.llm.context_window_tokens = window
    config.llm.max_tokens = 4096
    config.llm.thinking = "off"
    config.compaction.enabled = True
    config.compaction.protected_recent_messages = 4
    config.squilla_router = SquillaRouterConfig(
        rollout_phase="full", preset_binding="custom", auto_thinking=False,
        tiers={f"c{i}": {"provider": "openrouter", "model": model} for i in range(4)},
    )
    assert config.squilla_router.strategy == "lightgbm_model"
    classifier = SimpleNamespace(predict=lambda _: SimpleNamespace(
        intent_probabilities={"continue": 1.0, "redo": 0.0, "new_task": 0.0},
        tier_probabilities={f"c{i}": float(i == 1) for i in range(4)},
    ))
    monkeypatch.setattr(squilla_router, "_get_strategy", lambda _: classifier)
    catalog = ModelCatalog()
    config.models = {"openrouter": {model: ModelOverrideConfig(
        context_window=window, max_output_tokens=4096,
    )}}
    catalog._models[model] = ModelInfo(
        provider="openrouter", model_id=model, display_name=model,
        context_window=window, max_output_tokens=4096,
        supports_reasoning=False, supports_tools=True, supports_vision=False,
    )
    catalog.set_user_overrides({
        f"openrouter/{model}": config.models["openrouter"][model].model_dump(exclude_none=True),
    })
    monkeypatch.setattr(model_catalog, "_shared_catalog", catalog)
    facts = {
        "constraint": "Do not rename public endpoints.",
        "artifact": "reports/router-review.json",
        "pending": "Run the SQLite migration check.",
    }
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        text = json.dumps(payload["messages"], ensure_ascii=False)
        assert "OLD TASK SECRET" not in text
        assert "OLD CHECKPOINT SECRET" not in text
        assert "FUTURE QUEUED SECRET" not in text
        available = {key: value for key, value in facts.items() if value in text}
        seen.append((payload, available))
        frame = {
            "id": "generation-response", "model": model,
            "choices": [{"index": 0, "delta": {"content": json.dumps(available)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200},
        }
        return httpx.Response(
            200, request=request, headers={"content-type": "text/event-stream"},
            content=("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode(),
        )

    observer = harness.WireObserver(endpoint, transport=httpx.MockTransport(respond), max_calls=8)
    async with SessionStorage(tmp_path / "generation.sqlite") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:scoped-generation-summary")
        key = node.session_key
        await manager.append_message(key, "user", "OLD TASK SECRET")
        await storage.save_summary(SessionSummary(
            session_id=node.session_id, session_key=key, summary_text="OLD CHECKPOINT SECRET",
        ))
        unit = " log 17;"
        repeats = 48000 // (6 * estimate_tokens(unit))
        reset = None
        for index in range(6):
            prefix = json.dumps({"constraint": facts["constraint"]}) + "\n" if index == 0 else ""
            entry = await manager.append_message(key, "user", prefix + unit * repeats)
            if index == 0:
                reset = entry
                decision = envelope(entry.message_id, generation=entry.message_id)
                decision["state"] = ModelPolicyState(
                    tier="c1", task_id=entry.message_id,
                    task_start_input_message_id=entry.message_id,
                    generation_start_input_message_id=entry.message_id,
                ).to_dict()
                await manager.update_message_turn_context(key, entry.message_id, {
                    "router_model_decision": decision,
                })
            # Early discovered facts live in an assistant reply, which the
            # old user-prioritizing temporary window can omit under pressure.
            reply = (json.dumps({key: value for key, value in facts.items() if key != "constraint"})
                     if index == 0 else "Disposable logs acknowledged.")
            await manager.append_message(key, "assistant", reply)
        assert reset is not None
        await manager.append_message(key, "user", "Inspect the current specification.")
        await manager.append_message(key, "assistant", "", tool_calls=[{
            "type": "tool_use", "tool_use_id": "generation-read", "name": "read_file",
            "input": {"path": "spec.txt"},
        }, {
            "type": "tool_result", "tool_use_id": "generation-read", "name": "read_file",
            "result": "The current specification is available.",
        }])
        await manager.append_message(key, "assistant", "Specification inspected.")
        original = harness._canonical_message_digests(await manager.get_canonical_transcript(key))
        prompt = "Continue: return the original constraint, artifact and pending action as JSON."
        current = await manager.append_message(key, "user", prompt)
        await manager.append_message(key, "user", "FUTURE QUEUED SECRET")
        runner = TurnRunner(
            provider_selector=ModelSelector(SelectorConfig(primary=ProviderConfig(
                provider="openrouter", model=model, api_key="synthetic-openrouter",
                base_url=endpoint,
            ))),
            tool_registry=ToolRegistry(), session_manager=manager, config=config,
            model_catalog=catalog,
        )
        prepared = []
        original_preflight = runner._maybe_preflight_compact

        @functools.wraps(original_preflight)
        async def capture_preflight(*args, **kwargs):
            candidate = await original_preflight(*args, **kwargs)
            if candidate is not None:
                rows = [runner._entry_for_emergency_compaction(row)
                        for row in candidate.kept_entries]
                assert kwargs["compaction_budget"].consumer_admission(candidate.summary, rows)
                prepared.append(candidate)
            return candidate

        monkeypatch.setattr(runner, "_maybe_preflight_compact", capture_preflight)
        with observer.observe():
            events = [event async for event in runner.run(
                prompt, session_key=key, bound_user_message_id=current.message_id,
                tool_context=ToolContext(is_owner=True, workspace_dir=config.workspace_dir),
            )]
        assert not [event for event in events if event.kind == "error"]
        payload, visible_facts = seen[-1]
        assert visible_facts == facts
        summary_calls = [call for call in observer.calls if harness._is_compaction_wire_call(call)]
        assert summary_calls, "A new generation must summarize its own early facts before trimming"
        assert prepared and all(value in prepared[-1].summary for value in facts.values())
        assert estimate_tokens(json.dumps(payload)) + config.llm.max_tokens < window
        calls = [call["id"] for message in payload["messages"]
                 for call in message.get("tool_calls", [])]
        results = [message["tool_call_id"] for message in payload["messages"]
                   if message["role"] == "tool"]
        assert calls == results == ["generation-read"]
        canonical = harness._canonical_message_digests(await manager.get_canonical_transcript(key))
        assert all(canonical.get(message_id) == digest for message_id, digest in original.items())
        assert [row.summary_text for row in await manager.get_summaries(key)] == [
            "OLD CHECKPOINT SECRET",
        ]
