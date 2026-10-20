"""Production manual/preflight entrypoints carry only verifiable Store authority."""

from __future__ import annotations

import functools
import json
import time

import httpx
import pytest

from opensquilla.application.session_maintenance import CompactSession, SessionCompactionSession
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.tool_result_store import ToolResultStore
from opensquilla.gateway.adapters.session_maintenance import GatewaySessionMaintenancePorts
from opensquilla.gateway.config import ModelOverrideConfig
from opensquilla.gateway.rpc.registry import RpcContext
from opensquilla.provider import model_catalog
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import ModelInfo
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.builtin import tool_results as _tool_results  # noqa: F401
from opensquilla.tools.registry import ToolRegistry, get_default_registry
from opensquilla.tools.types import ToolContext
from scripts import live_reasoning_replay_e2e as harness


async def prepare(tmp_path, monkeypatch, *, repeats=4000, foreign=False):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-unused")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    config = harness._config(tmp_path, "openrouter", "refs-primary", "https://example.invalid/v1",
                             thinking="off")
    config.llm.context_window_tokens = 0
    config.llm.max_tokens = 2048
    config.compaction.enabled = True
    config.compaction.protected_recent_messages = 1
    config.tools.allow = ["retrieve_tool_result", "tool_search"]
    catalog = ModelCatalog()
    config.models = {"openrouter": {}}
    for model, window in (("refs-primary", 32768), ("refs-large", 131072)):
        override = ModelOverrideConfig(context_window=window, max_output_tokens=2048)
        config.models["openrouter"][model] = override
        catalog._models[model] = ModelInfo(
            provider="openrouter", model_id=model, display_name=model,
            context_window=window, max_output_tokens=2048, supports_reasoning=False,
            supports_tools=True, supports_vision=False,
        )
    catalog.set_user_overrides({
        f"openrouter/{model}": override.model_dump(exclude_none=True)
        for model, override in config.models["openrouter"].items()
    })
    monkeypatch.setattr(model_catalog, "_shared_catalog", catalog)
    registry = ToolRegistry()
    for name in ("retrieve_tool_result", "tool_search"):
        registered = get_default_registry().get(name)
        assert registered is not None
        registry.register(registered.spec, registered.handler)
    configs = [ProviderConfig(provider="openrouter", model=model, api_key="synthetic-unused",
                              base_url="https://example.invalid/v1")
               for model in ("refs-primary", "refs-large")]
    selector = ModelSelector(SelectorConfig(primary=configs[0], fallbacks=[configs[1]]))
    storage = SessionStorage(str(tmp_path / "sessions.sqlite"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False,
                             checkpoint_workspace_dir=config.workspace_dir)
    node = await manager.create("agent:main:refs-production")
    store = ToolResultStore(tmp_path / "media" / "tool-results")
    original = "TASK_BETA_ORIGINAL evidence observation line.\n" * repeats
    record = store.write(original, tool_use_id="evidence-call", tool_name="read_log",
                         session_id="other-session" if foreign else node.session_id,
                         session_key=node.session_key, agent_id="main")
    inline = (f"[tool_result_projection]\ntool_result_handle: {record.handle}\n"
              f"sha256: {record.sha256}\n" + original)
    await manager.append_message(node.session_key, "user", "Inspect Task Beta evidence.")
    await manager.append_message(node.session_key, "assistant", "", tool_calls=[
        {"type": "tool_use", "tool_use_id": "evidence-call", "name": "read_log", "input": {}},
        {"type": "tool_result", "tool_use_id": "evidence-call", "result": inline},
    ])
    await manager.append_message(node.session_key, "assistant", "Inspection finished.")
    await manager.append_message(node.session_key, "user", "Task Alpha is finished.")
    await manager.append_message(node.session_key, "assistant", "Continue Task Beta only.")
    active = await manager.append_message(node.session_key, "user", "Continue Task Beta.")
    runner = TurnRunner(provider_selector=selector, tool_registry=registry, session_manager=manager,
                        config=config, model_catalog=catalog)
    # Keep unrelated profile files outside the fixture; production tools, owner,
    # Agent creation, preflight stage and writer all remain real.
    monkeypatch.setattr(runner, "_assemble_prompt", lambda *args, **kwargs: "Help with Task Beta.")
    return config, runner, manager, node, active, store, record, original


def wire(monkeypatch, *, reject_primary_summary=False):
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        is_summary = any("conversation compactor" in str(message.get("content", ""))
                         for message in payload["messages"] if message["role"] == "system")
        if is_summary and reject_primary_summary and payload["model"] == "refs-primary":
            return httpx.Response(400, json={"error": {
                "message": "maximum context length exceeded", "code": "context_length_exceeded",
                "type": "invalid_request_error",
            }})
        frame = {"choices": [{"delta": {"content": "Task Alpha complete. Continue Task Beta."},
                               "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n")

    client = httpx.AsyncClient
    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", lambda **kwargs: client(
        **{**kwargs, "transport": httpx.MockTransport(respond)},
    ))
    return calls


def summary_calls(calls):
    return [call for call in calls if any(
        "conversation compactor" in str(message.get("content", ""))
        for message in call["messages"] if message["role"] == "system"
    )]


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, True])
async def test_real_turn_preflight_discovers_summary_capacity_chain_and_store_owner(
    tmp_path, monkeypatch, foreign,
):
    config, runner, manager, node, active, store, record, original = await prepare(
        # Overflow the 32K consumer while fitting the larger summarizer's
        # bounded input, so both verified projection and raw foreign-result
        # ownership are exercised without switching to sampled-history format.
        tmp_path, monkeypatch, repeats=3000, foreign=foreign,
    )
    calls = wire(monkeypatch, reject_primary_summary=True)
    seen = []
    preflight = runner._maybe_preflight_compact

    @functools.wraps(preflight)
    async def capture(*args, **kwargs):
        seen.append(kwargs)
        return await preflight(*args, **kwargs)

    monkeypatch.setattr(runner, "_maybe_preflight_compact", capture)
    before = harness._canonical_message_digests(
        await manager.get_canonical_transcript(node.session_key),
    )
    try:
        events = [event async for event in runner.run(
            "Continue Task Beta.", node.session_key,
            ToolContext(is_owner=True, workspace_dir=config.workspace_dir),
            bound_user_message_id=active.message_id,
            expected_session_id=node.session_id, expected_session_epoch=node.epoch,
        )]
        assert not [event for event in events if event.kind == "error"]
        assert seen and [target.model for target in seen[0]["compaction_plan"].candidates] == [
            "refs-primary", "refs-large",
        ]
        context = seen[0]["compaction_request_context"]
        agent = context.recoverable_reference_resolver.__self__
        assert agent.config.tool_result_store_session_id == node.session_id
        assert agent._tool_result_recovery_available()
        summaries = summary_calls(calls)
        assert [call["model"] for call in summaries] == ["refs-primary", "refs-large"]
        for call in summaries:
            tools = [message for message in call["messages"] if message["role"] == "tool"]
            assert tools and tools[0]["tool_call_id"] == "evidence-call"
            assert record.handle in tools[0]["content"] and record.sha256 in tools[0]["content"]
            if foreign and call["model"] == "refs-large":
                # A larger model can see the complete unverified raw result.
                # Projection size need not shrink monotonically across requests.
                assert original in tools[0]["content"]
            else:
                assert original not in tools[0]["content"]
                assert ("unverified and not usable for recall" in tools[0]["content"]) is foreign
        assert calls[-1]["model"] == "refs-primary"  # Summary did not advance execution.
        assert await manager.get_summaries(node.session_key)
        after = harness._canonical_message_digests(
            await manager.get_canonical_transcript(node.session_key),
        )
        assert all(after.get(key) == value for key, value in before.items())
        if foreign:
            with pytest.raises(FileNotFoundError):
                store.read(record.handle, session_id=node.session_id)
        assert store.read(record.handle, session_id=record.session_id).content == original
    finally:
        await manager.storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("pressure", [False, True])
async def test_manual_idle_plan_does_not_invent_store_retrieval_authority(
    tmp_path, monkeypatch, pressure,
):
    config, runner, manager, node, _active, store, record, original = await prepare(
        tmp_path, monkeypatch, repeats=4000 if pressure else 100,
    )
    calls = wire(monkeypatch)
    ports = GatewaySessionMaintenancePorts(RpcContext(
        conn_id="manual-refs", config=config, session_manager=manager,
        provider_selector=runner._provider_selector, turn_runner=runner,
    ))
    plan = ports.build_plan(SessionCompactionSession(node.session_id, "main", node), None,
                            "manual-refs", time.monotonic() + 60)
    agent = plan.runtime_value.config.request_context.recoverable_reference_resolver.__self__
    assert not agent._tool_result_recovery_available()
    before = harness._canonical_message_digests(
        await manager.get_canonical_transcript(node.session_key),
    )
    try:
        result = await ports._compact(CompactSession(node.session_key), plan)
        assert result.applied
        summaries = summary_calls(calls)
        tools = [message for call in summaries for message in call["messages"]
                 if message["role"] == "tool"]
        assert tools and tools[0]["tool_call_id"] == "evidence-call"
        if pressure:
            assert "unverified and not usable for recall" in tools[0]["content"]
            assert original not in tools[0]["content"]
        else:
            assert original in tools[0]["content"]
        after = harness._canonical_message_digests(
            await manager.get_canonical_transcript(node.session_key),
        )
        assert all(after.get(key) == value for key, value in before.items())
        assert store.read(record.handle, session_id=node.session_id).content == original
    finally:
        await manager.storage.close()
