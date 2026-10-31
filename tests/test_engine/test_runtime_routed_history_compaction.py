"""Smaller routes summarize history before the final adapter can elide it."""

from __future__ import annotations

import functools
import json

import httpx
import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import ModelOverrideConfig, SquillaRouterConfig
from opensquilla.provider import model_catalog
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import ModelInfo
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.token_estimation import estimate_tokens
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext
from scripts import live_reasoning_replay_e2e as harness


def _facts_in_wire_messages(messages, facts):
    visible = "\n".join(
        message["content"] if isinstance(message.get("content"), str)
        else json.dumps(message.get("content"), ensure_ascii=False)
        for message in messages
    )
    # Bounded input quotes source rows as JSON, then may excerpt the row before
    # its closing quote. Match the same exact key/value pair in either wire
    # representation; requiring the whole excerpt to parse would invent loss.
    return {
        key: value for key, value in facts.items()
        if any(fragment in visible for fragment in (
            json.dumps({key: value}, ensure_ascii=False)[1:-1],
            json.dumps(json.dumps({key: value}, ensure_ascii=False)[1:-1],
                       ensure_ascii=False)[1:-1],
        ))
    }


@pytest.mark.parametrize("quoted", [False, True, "excerpt"])
def test_wire_fact_oracle_handles_quoted_records_without_inventing_missing_values(quoted):
    facts = harness.compaction_task_facts("chinese_mix")
    supplied = {key: value for key, value in facts.items() if key != "pending"}
    supplied["artifact"] = "docs/incorrect.md"
    content = json.dumps(supplied, ensure_ascii=False)
    if quoted:
        content = json.dumps(
            {"role": "user", "content": content + "\nDisposable logs." * 20},
            ensure_ascii=False,
        )
        if quoted == "excerpt":
            content = content[:-40]
        content = "<historical-excerpts>\n" + content + "\n</historical-excerpts>"
    observed = _facts_in_wire_messages([{"role": "user", "content": content}], facts)
    assert observed == {key: value for key, value in facts.items()
                        if key not in {"pending", "artifact"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "routed_window,history_tokens,expect_compaction",
    [(32768, 48000, True), (131072, 150000, True), (32768, 8000, False)],
)
async def test_small_route_preserves_old_task_facts_before_final_projection(
    tmp_path, monkeypatch, routed_window, history_tokens, expect_compaction,
):
    """Same pressure/facts as the live routing regressions, with a fake wire.

    Replies only repeat facts actually present in each request. This verifies
    the history/summary plumbing, not a language model's retention quality.
    """
    monkeypatch.setenv("TOKENRHYTHM_API_KEY", "synthetic-tokenrhythm")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-openrouter")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    base_model = "qwen3.8-flash"
    routed_model = (
        "qwen/qwen-2.5-coder-32b-instruct"
        if routed_window == 32768 else "mistralai/mistral-nemo"
    )
    endpoints = {name: harness.registry_endpoint(name) for name in ("tokenrhythm", "openrouter")}
    config = harness._config(tmp_path, "tokenrhythm", base_model, endpoints["tokenrhythm"])
    config.llm.context_window_tokens = 1_000_000
    config.llm.max_tokens = 4096
    config.compaction.enabled = True
    config.preflight_compact_ratio = 0.8
    config.squilla_router = SquillaRouterConfig(
        strategy="legacy_v4_phase3",
        enabled=True, cross_provider_tiers=True, preset_binding="custom",
        tiers={
            f"c{index}": {
                "provider": "openrouter", "model": routed_model, "ensemble_enabled": False,
            }
            for index in range(4)
        },
        auto_thinking=False, kv_cache_anti_downgrade_enabled=False, require_router_runtime=False,
    )
    catalog = ModelCatalog()
    config.models = {}
    for provider, model, window in (
        ("tokenrhythm", base_model, 1_000_000),
        ("openrouter", routed_model, routed_window),
    ):
        config.models.setdefault(provider, {})[model] = ModelOverrideConfig(
            context_window=window, max_output_tokens=4096,
        )
        catalog._models[model] = ModelInfo(
            provider=provider, model_id=model, display_name=model,
            context_window=window, max_output_tokens=4096,
            supports_reasoning=False, supports_tools=True, supports_vision=False,
        )
    catalog.set_user_overrides({
        f"{provider}/{model}": override.model_dump(exclude_none=True)
        for provider, models in config.models.items() for model, override in models.items()
    })
    monkeypatch.setattr(model_catalog, "_shared_catalog", catalog)

    facts = harness.compaction_task_facts("chinese_mix")
    wire_facts = []

    def respond(request):
        payload = json.loads(request.content)
        assert payload["model"] == routed_model
        available = _facts_in_wire_messages(payload["messages"], facts)
        wire_facts.append(available)
        frame = {
            "id": "synthetic-route", "model": payload["model"],
            "choices": [{
                "index": 0, "delta": {"content": json.dumps(available, ensure_ascii=False)},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200},
        }
        return httpx.Response(
            200, request=request, headers={"content-type": "text/event-stream"},
            content=("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode(),
        )

    observer = harness.WireObserver(
        endpoints["tokenrhythm"], endpoints=endpoints,
        transport=httpx.MockTransport(respond), max_calls=12,
    )
    storage = SessionStorage(str(tmp_path / "sessions.sqlite"))
    await storage.connect()
    manager = SessionManager(
        storage, inject_time_prefix=False, checkpoint_workspace_dir=config.workspace_dir,
    )
    key = "agent:main:route-continuity"
    preflight = []
    admission_results = []
    tail_capacity_results = []
    try:
        await manager.create(session_key=key, agent_id="main")
        unit = " log 17;"
        repeats = history_tokens // (6 * estimate_tokens(unit))
        for index in range(6):
            prefix = (
                harness.compaction_task_instructions("chinese_mix")
                + "\n" + json.dumps(facts, ensure_ascii=False) + "\n"
                if index == 0 else ""
            )
            await manager.append_message(key, "user", prefix + unit * repeats)
            await manager.append_message(key, "assistant", "Disposable logs acknowledged.")
        original_entries = await manager.get_canonical_transcript(key)
        original = harness._canonical_message_digests(original_entries)
        prompt = "Return the original task values as JSON: " + ", ".join(facts)
        active = await manager.append_message(key, "user", prompt)
        runner = TurnRunner(
            provider_selector=ModelSelector(SelectorConfig(primary=ProviderConfig(
                provider="tokenrhythm", model=base_model, api_key="synthetic-tokenrhythm",
                base_url=endpoints["tokenrhythm"],
            ))),
            tool_registry=ToolRegistry(), session_manager=manager,
            config=config, model_catalog=catalog,
        )
        original_preflight = runner._maybe_preflight_compact

        @functools.wraps(original_preflight)
        async def capture_preflight(*args, **kwargs):
            preflight.append((args, kwargs))
            admission = kwargs["compaction_budget"].consumer_admission
            admission_results.append((
                admission("retained task", []),
                admission("log 17; " * routed_window, []),
            ))
            tail_proof = kwargs["compaction_budget"].consumer_tail_over_capacity
            assert tail_proof is not None
            tail_capacity_results.append((
                tail_proof([]),
                tail_proof([
                    {"role": "user", "content": "log 17; " * routed_window},
                    active.model_dump(),
                ]),
            ))
            return await original_preflight(*args, **kwargs)

        monkeypatch.setattr(runner, "_maybe_preflight_compact", capture_preflight)
        with observer.observe():
            events = [event async for event in runner.run(
                prompt, session_key=key, bound_user_message_id=active.message_id,
                tool_context=ToolContext(is_owner=True, workspace_dir=config.workspace_dir),
            )]
        assert not [event for event in events if event.kind == "error"]
        assert wire_facts[-1] == facts  # Facts must reach the actual final adapter request.
        assert preflight
        args, kwargs = preflight[0]
        assert args[1] == 1_000_000  # Stable durable ownership is unchanged.
        budget = kwargs["compaction_budget"]
        assert budget.physical_context_window_tokens == routed_window
        assert 0 < budget.history_capacity_tokens < routed_window
        assert admission_results == [(True, False)]
        # The smaller routed consumer must participate in the exact tail
        # proof, even though durable ownership remains on the 1M deployment.
        assert tail_capacity_results == [(False, True)]
        summaries = await manager.get_summaries(key)
        summary_calls = [call for call in observer.calls if harness._is_compaction_wire_call(call)]
        assert bool(summaries) is expect_compaction
        assert bool(summary_calls) is expect_compaction
        if expect_compaction:
            assert all(value in summaries[-1].summary_text for value in facts.values())
            # The newest complete round crosses the routed window's soft 16%
            # target. Keep it verbatim, along with the current prompt, rather
            # than dropping it or choosing a tail sized for the stable 1M model.
            expected_raw = original_entries[-2:]
            assert sum(summary.removed_count for summary in summaries) == len(original) - 2
            active_entries = await manager.get_transcript(key)
            raw_tail = [entry for entry in active_entries if entry.message_id in original]
            assert [entry.message_id for entry in raw_tail] == [
                entry.message_id for entry in expected_raw
            ]
            assert harness._canonical_message_digests(raw_tail) == {
                entry.message_id: original[entry.message_id] for entry in expected_raw
            }
            execution_calls = [
                call for call in observer.calls if not harness._is_compaction_wire_call(call)
            ]
            assert execution_calls
            for entry in expected_raw:
                assert any(
                    message.get("role") == entry.role
                    and isinstance(message.get("content"), str)
                    and entry.content in message["content"]
                    for message in execution_calls[-1].request["messages"]
                )
        canonical = harness._canonical_message_digests(await manager.get_canonical_transcript(key))
        assert all(canonical.get(message_id) == digest for message_id, digest in original.items())
    finally:
        await storage.close()
