"""Idle summaries freeze the ordinary consumer controls and circuit scope."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from opensquilla.application.session_maintenance import (
    CompactSession,
    SessionCompactionExecutionResult,
    SessionCompactionSession,
)
from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.adapters.session_maintenance import GatewaySessionMaintenancePorts
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc.registry import RpcContext
from opensquilla.provider.protocol import project_provider_final_request
from opensquilla.provider.request_proof import projected_generation_budget
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.types import ToolDefinition
from opensquilla.session.compaction import _build_suffix_compaction_call
from opensquilla.session.models import SessionNode


def _manual(monkeypatch, *, provider="openai", model="synthetic-current", explicit_cap=0):
    config = GatewayConfig(llm={
        "provider": provider, "model": model, "api_key": "synthetic-key",
        "context_window_tokens": 64_000, "max_tokens": 1024, "thinking": "medium",
        "provider_request_proof_max_chars": explicit_cap,
    })
    current = ProviderConfig(provider=provider, model=model, api_key="synthetic-key")
    runner = TurnRunner(provider_selector=None, config=config)
    tool = ToolDefinition(name="synthetic_lookup", description="Inert historical schema.",
                          input_schema={"type": "object", "properties": {}})
    monkeypatch.setattr(runner, "_build_tools", lambda **_: ([tool], None))
    monkeypatch.setattr(runner, "_assemble_prompt", lambda *_, **__: "Ordinary task instructions.")
    monkeypatch.setattr(runner, "_extra_context_for_tool_context", lambda _: None)
    captured = []
    prepare = runner.prepare_manual_compaction_envelope

    def capture(*args, **kwargs):
        agent = prepare(*args, **kwargs)
        captured.append(agent)
        return agent

    monkeypatch.setattr(runner, "prepare_manual_compaction_envelope", capture)
    raw = SessionNode(
        session_key="agent:main:webchat:manual-controls", session_id="manual-controls",
    )
    ports = GatewaySessionMaintenancePorts(RpcContext(
        conn_id="manual-controls", config=config, turn_runner=runner,
        session_manager=SimpleNamespace(storage=None),
        provider_selector=SimpleNamespace(current_config=current),
    ))
    plan = ports.build_plan(
        SessionCompactionSession(raw.session_id, "main", raw), None,
        "manual-controls", time.monotonic() + 120,
    )
    return ports, plan.runtime_value, plan, runner, config, captured[0], current, raw


@pytest.mark.parametrize(("provider", "model", "wire_cap"), [
    ("openai", "synthetic-current", 1024),
    ("anthropic", "claude-3-7-sonnet-latest", 14_096),
])
@pytest.mark.parametrize("explicit_cap", [0, 100_000])
def test_manual_freezes_current_generation_and_reasoning_controls(
    monkeypatch, provider, model, wire_cap, explicit_cap,
):
    _, runtime, _, _, gateway, agent, _, _ = _manual(
        monkeypatch, provider=provider, model=model, explicit_cap=explicit_cap,
    )
    context = runtime.config.request_context
    assert context is not None
    current = agent.build_compaction_request_context().chat_config
    assert context.chat_config == current
    assert current.max_tokens == runtime.budget.max_output_tokens == 1024
    assert current.thinking is True
    assert current.thinking_budget_tokens == 10_000
    assert current.provider_request_max_chars_explicit_cap == explicit_cap
    assert current.model_capabilities is not None
    assert runtime.config.llm_plan.primary.max_generation_tokens == current.max_tokens

    # The summary builder owns its purpose; controls alone come from this idle
    # consumer. Business system/stop/schema constraints must not bleed through.
    context.chat_config.stop_sequences = ["BUSINESS_STOP"]
    context.chat_config.output_json_schema = {"type": "object"}
    context.chat_config.tool_choice = "required"
    messages, tools, writer = _build_suffix_compaction_call(
        context, [{"role": "user", "content": "Remember the completed work."}],
        "", "", None, provider=runtime.budget.provider, context_window_tokens=64_000,
        summary_output_tokens=runtime.config.llm_plan.primary.max_output_tokens,
        timeout=30, provider_request_correlation=None,
        deployment=runtime.config.llm_plan.primary,
    )
    assert writer.max_tokens == 1024
    assert writer.thinking_budget_tokens == 10_000
    assert writer.thinking_level == current.thinking_level
    assert writer.model_capabilities == current.model_capabilities
    assert writer.provider_request_max_chars_explicit_cap == explicit_cap
    assert "Ordinary task instructions." not in writer.system
    assert writer.stop_sequences == []
    assert writer.output_json_schema is None
    assert writer.tool_choice == "none"
    projection = project_provider_final_request(runtime.budget.provider, messages, tools, writer)
    assert projected_generation_budget(projection.payload, writer.max_tokens) == wire_cap
    assert runtime.config.budget.generation_reserve_tokens == wire_cap
    assert runtime.budget.generation_reserve_tokens == wire_cap
    gateway.llm.max_tokens = 8000
    assert context.chat_config.max_tokens == 1024  # detached accepted operation


async def test_manual_model_switch_failures_survive_next_ordinary_scope_bind(monkeypatch):
    ports, runtime, plan, runner, _, agent, current, raw = _manual(monkeypatch)
    key = raw.session_key
    runner._bind_compaction_failure_scope(key, ("old-model-A",))
    runner._record_compaction_failure(key)

    async def fail_summary(*_):
        runtime.config.on_summary_call_started()
        return SessionCompactionExecutionResult(
            applied=False, summary_len=0, skip_reason="summary_failed",
        )

    monkeypatch.setattr(ports, "_compact", fail_summary)
    for expected in (1, 2, 3):
        await ports.compact(CompactSession(key), plan)
        assert runner._compaction_failures[key].count == expected
    assert runner._compaction_circuit_open(key)

    # The same physical provider can have a normalized connection URL while
    # ProviderConfig has no explicit URL. A different derived character
    # estimate is also not a new actual constraint.
    ordinary = agent.build_compaction_request_context().chat_config.model_copy(update={
        "provider_request_max_chars": 123_456,
    })
    ordinary_scope = runner._compaction_failure_identity(
        provider=runtime.budget.provider, provider_config=current, chat_config=ordinary,
        policy=runner._compaction_failure_policy(agent.config),
    )
    assert ordinary_scope == runtime.failure_scope
    runner._bind_compaction_failure_scope(key, ordinary_scope)
    assert runner._compaction_failures[key].count == 3
    assert runner._compaction_circuit_open(key)


async def test_no_dispatch_manual_refusal_does_not_rebind_or_erase_old_failure(monkeypatch):
    ports, _, plan, runner, _, _, _, raw = _manual(monkeypatch)
    key = raw.session_key
    runner._bind_compaction_failure_scope(key, ("old-model-A",))
    runner._record_compaction_failure(key)
    scope = runner._compaction_failure_scopes[key]

    async def refuse_before_dispatch(*_):
        return SessionCompactionExecutionResult(
            applied=False, summary_len=0, skip_reason="summary_failed",
        )

    monkeypatch.setattr(ports, "_compact", refuse_before_dispatch)
    await ports.compact(CompactSession(key), plan)
    assert runner._compaction_failure_scopes[key] == scope
    assert runner._compaction_failures[key].count == 1
