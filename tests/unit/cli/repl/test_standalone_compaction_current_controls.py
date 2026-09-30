"""Standalone manual summaries retain the accepted consumer's generation controls."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.cli.chat.session_state import ChatSessionState
from opensquilla.cli.tui.adapters.slash_standalone import (
    StandaloneSlashContext,
    StandaloneSlashServices,
    handle_standalone_slash_command,
)
from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.protocol import project_provider_final_request
from opensquilla.provider.request_proof import projected_generation_budget
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.types import ToolDefinition
from opensquilla.session.compaction import _build_suffix_compaction_call
from opensquilla.session.models import SessionNode


@pytest.mark.parametrize(("provider", "model", "wire_cap"), [
    ("openai", "synthetic-current", 1024),
    ("anthropic", "claude-3-7-sonnet-latest", 14_096),
])
@pytest.mark.parametrize("explicit_cap", [0, 100_000])
async def test_standalone_compaction_freezes_current_generation_controls(
    monkeypatch, provider, model, wire_cap, explicit_cap,
):
    config = GatewayConfig(llm={
        "provider": provider, "model": model, "api_key": "synthetic-key",
        "context_window_tokens": 64_000, "max_tokens": 1024, "thinking": "medium",
        "provider_request_proof_max_chars": explicit_cap,
    })
    current = ProviderConfig(provider=provider, model=model, api_key="synthetic-key")
    runner = TurnRunner(provider_selector=None, config=config)
    tool = ToolDefinition(name="synthetic_lookup", description="Historical business schema.",
                          input_schema={"type": "object", "properties": {}})
    monkeypatch.setattr(runner, "_build_tools", lambda **_: ([tool], None))
    monkeypatch.setattr(runner, "_assemble_prompt", lambda *_, **__: "Ordinary task instructions.")
    monkeypatch.setattr(runner, "_extra_context_for_tool_context", lambda _: None)
    captured = []
    prepare = runner.prepare_manual_compaction_envelope

    def capture(*args, **kwargs):
        agent = prepare(*args, **kwargs)
        captured.append((agent, kwargs))
        return agent

    monkeypatch.setattr(runner, "prepare_manual_compaction_envelope", capture)
    node = SessionNode(session_key="agent:main:standalone:current-controls",
                       session_id="current-controls")
    accepted = []

    async def compact(_key, _window, compaction_config):
        accepted.append(compaction_config)
        return "A valid summary of completed work."

    state = ChatSessionState(session_key=node.session_key, model=None)
    slash = StandaloneSlashContext(
        state=state, session_key=node.session_key, model=None, tool_ctx=object(),
        slash_services=StandaloneSlashServices(
            get_session=lambda _: node, compact_session=compact, config=config,
            provider_selector=SimpleNamespace(current_config=current),
        ),
        turn_runner=runner, build_tool_ctx=lambda _: object(),
        replace_session=lambda **_: None,
    )

    assert await handle_standalone_slash_command("/compact", slash) is True
    assert len(accepted) == len(captured) == 1
    compaction = accepted[0]
    consumer, options = captured[0]
    context = compaction.request_context
    assert context is not None
    ordinary = consumer.build_compaction_request_context().chat_config
    assert context.chat_config == ordinary
    assert ordinary.max_tokens == 1024
    assert ordinary.thinking is True
    assert ordinary.thinking_budget_tokens == 10_000
    assert ordinary.provider_request_max_chars_explicit_cap == explicit_cap
    assert compaction.llm_plan.primary.provider is options["provider"]
    assert compaction.llm_plan.primary.max_generation_tokens == ordinary.max_tokens

    # The shared builder takes generation controls from the accepted consumer,
    # while summary purpose strips task-only constraints and instructions.
    context.chat_config.stop_sequences = ["BUSINESS_STOP"]
    context.chat_config.output_json_schema = {"type": "object"}
    context.chat_config.tool_choice = "required"
    messages, tools, writer = _build_suffix_compaction_call(
        context, [{"role": "user", "content": "Remember the completed work."}],
        "", "", None, provider=options["provider"], context_window_tokens=64_000,
        summary_output_tokens=compaction.llm_plan.primary.max_output_tokens,
        timeout=30, provider_request_correlation=None,
        deployment=compaction.llm_plan.primary,
    )
    assert writer.max_tokens == 1024
    assert writer.thinking_budget_tokens == 10_000
    assert writer.thinking_level == ordinary.thinking_level
    assert writer.model_capabilities == ordinary.model_capabilities
    assert writer.provider_request_max_chars_explicit_cap == explicit_cap
    assert "Ordinary task instructions." not in writer.system
    assert writer.stop_sequences == []
    assert writer.output_json_schema is None
    assert writer.tool_choice == "none"
    projection = project_provider_final_request(options["provider"], messages, tools, writer)
    assert projected_generation_budget(projection.payload, writer.max_tokens) == wire_cap
    assert compaction.budget.generation_reserve_tokens == wire_cap
    config.llm.max_tokens = 8000
    assert context.chat_config.max_tokens == 1024
