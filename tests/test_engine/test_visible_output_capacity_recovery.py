"""Visible length continuations recover a lowered wire cap without replaying work."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig, DoneEvent, ErrorEvent, ToolResult
from opensquilla.engine.types import CompactionEvent
from opensquilla.provider import (
    Message,
    ModelCapabilities,
    OpenAIProvider,
    ToolDefinition,
    request_proof,
)

PARTIAL = "The next required step is"
FINAL = "Finished all requested work."


def _case(monkeypatch, *, history_size=60_000, protected_only=False,
          cheap_continuation=False, followup_tool=False, recovery_failure=None,
          required_cap=110_000, distinct_partials=False):
    monkeypatch.setattr(
        request_proof, "_serialized_token_estimate",
        lambda text: ((len(text) + 3) // 4, "controlled_full_payload"),
    )
    provider = OpenAIProvider(api_key="unused", model="glm-5.1",
                              provider_kind="tokenrhythm", base_url="https://example.invalid/v1")
    executed = []

    async def handle_tool(call):
        executed.append(call.tool_use_id)
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name,
                          content="VERIFIED-RECORD-42" if call.tool_use_id == "read-once"
                          else "AUDIT-WRITTEN-ONCE")

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_id="glm-5.1", provider_id="tokenrhythm", context_window_tokens=200_000,
            context_window_known=True, max_tokens=128_000, thinking=True,
            model_capabilities=ModelCapabilities(supports_tools=True, supports_reasoning=True),
            max_provider_retries=1, retry_base_backoff_ms=0, retry_max_backoff_ms=0,
            timeout=30, iteration_timeout=30,
        ),
        tool_definitions=[ToolDefinition(name=name, description="Work on the existing record.",
                                        input_schema={"type": "object", "properties": {}})
                          for name in ("read_file", "write_file")],
        tool_handler=handle_tool,
    )
    history = []
    if not protected_only:
        for index in range(6):
            history.extend([
                Message(role="user", content=f"Historical request {index}"),
                Message(role="assistant", content="old log " + "x" * history_size),
            ])
    agent.set_history(history)
    frozen = deepcopy(history)
    current = "Read the record, finish the analysis, and write the requested audit."
    if protected_only:
        current += " Required current material: " + "y" * 360_000
    sent, configs, recovery_calls = [], {}, []
    original_build = provider._build_payload

    def observe_build(messages, tools, config, **kwargs):
        configs["latest"] = config
        return original_build(messages, tools, config, **kwargs)

    monkeypatch.setattr(provider, "_build_payload", observe_build)
    original_recover = agent._recover_progressed_request_window

    async def observe_recovery(*args, **kwargs):
        recovery_calls.append(kwargs)
        if recovery_failure == "cancel":
            raise asyncio.CancelledError
        if recovery_failure == "deadline":
            kwargs["chat_config"] = kwargs["chat_config"].model_copy(update={
                "turn_deadline_at_monotonic": asyncio.get_running_loop().time() - 1,
            })
        return await original_recover(*args, **kwargs)

    monkeypatch.setattr(agent, "_recover_progressed_request_window", observe_recovery)
    client = httpx.AsyncClient

    def handle(request):
        payload = json.loads(request.content)
        config = configs["latest"]
        proof = request_proof.project_provider_payload(
            payload, projection_adapter="exact_test_http",
            proof_budget=request_proof.provider_request_character_budget(payload, config),
            token_budget=request_proof.provider_request_token_budget(payload, config),
        )
        assert proof["fits"]
        assert current in json.dumps(payload)
        assert config.thinking is True and config.max_tokens == 128_000
        sent.append((payload, config, proof))
        cap = payload.get("max_completion_tokens", payload.get("max_tokens"))
        serialized = json.dumps(payload)
        if len(sent) == 1:
            delta = {"tool_calls": [{"index": 0, "id": "read-once", "type": "function",
                                    "function": {"name": "read_file", "arguments": "{}"}}]}
            stop, output, reasoning = "tool_calls", 10, 0
        elif len(sent) == 2 or (cap < required_cap and not cheap_continuation):
            partial = f"{PARTIAL} {len(sent) - 1}." if distinct_partials else PARTIAL
            delta = {"content": partial, "reasoning_content": "Considering the solution."}
            stop, output, reasoning = "length", cap, cap - 8
        elif followup_tool and "AUDIT-WRITTEN-ONCE" not in serialized:
            delta = {"tool_calls": [{"index": 0, "id": "write-once", "type": "function",
                                    "function": {"name": "write_file", "arguments": "{}"}}]}
            stop, output, reasoning = "tool_calls", 10, 0
        else:
            delta = {"content": FINAL}
            stop, output, reasoning = "stop", 10, 0
        body = {"choices": [{"delta": delta, "finish_reason": stop}],
                "usage": {"prompt_tokens": proof["estimated_tokens"], "completion_tokens": output,
                          "completion_tokens_details": {"reasoning_tokens": reasoning}}}
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n".encode(),
        )

    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(handle)}),
    )
    return agent, current, frozen, sent, executed, recovery_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("followup_tool", [False, True])
async def test_visible_length_installs_recovered_view_before_partial_tail(
    monkeypatch, followup_tool,
):
    agent, current, frozen, sent, executed, recovery = _case(
        monkeypatch, followup_tool=followup_tool,
    )
    events = [event async for event in agent.run_turn(current)]
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert any(isinstance(event, DoneEvent) and FINAL in event.text for event in events)
    assert len(sent) == (4 if followup_tool else 3)
    assert executed == (["read-once", "write-once"] if followup_tool else ["read-once"])
    assert len(recovery) == 1
    assert recovery[0]["increase_generation_budget"] is True
    caps = [payload["max_tokens"] for payload, _, _ in sent]
    assert caps[1] < caps[2] <= 128_000
    assert len(json.dumps(sent[2][0])) < len(json.dumps(sent[1][0]))
    for payload, _, _ in sent[2:]:
        assert json.dumps(payload).count(PARTIAL) == 1
        assert "VERIFIED-RECORD-42" in json.dumps(payload)
        calls = [call["id"] for message in payload["messages"]
                 for call in message.get("tool_calls", [])]
        results = [message["tool_call_id"] for message in payload["messages"]
                   if message.get("role") == "tool"]
        assert calls == results
    visible = [event.text for event in events if getattr(event, "kind", "") == "text_delta"]
    assert visible.count(PARTIAL) == 1 and visible.count(FINAL) == 1
    assert len({cfg.turn_deadline_at_monotonic for _, cfg, _ in sent}) == 1
    assert not any(isinstance(event, CompactionEvent) for event in events)
    assert agent.history_snapshot()[:len(frozen)] == frozen
    assert agent.config.max_tokens == 128_000 and agent.config.thinking is True


@pytest.mark.asyncio
async def test_repeated_visible_capacity_recovery_keeps_each_appended_partial(monkeypatch):
    agent, current, frozen, sent, executed, recovery = _case(
        monkeypatch, required_cap=127_000, distinct_partials=True,
    )
    events = [event async for event in agent.run_turn(current)]
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert len(recovery) == 2 and len(sent) == 4
    assert executed == ["read-once"]
    assert any(isinstance(event, DoneEvent) and FINAL in event.text for event in events)
    partials = [f"{PARTIAL} 1.", f"{PARTIAL} 2."]
    for index, (payload, _, _) in enumerate(sent[2:], start=1):
        text = json.dumps(payload)
        assert all(text.count(partial) == 1 for partial in partials[:index])
        assert "VERIFIED-RECORD-42" in text
    visible = [event.text for event in events if getattr(event, "kind", "") == "text_delta"]
    assert all(visible.count(partial) == 1 for partial in partials)
    assert agent.history_snapshot()[:len(frozen)] == frozen
    assert len({cfg.turn_deadline_at_monotonic for _, cfg, _ in sent}) == 1
    assert not any(isinstance(event, CompactionEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("history_size", [20, 60_000])
async def test_visible_prefix_can_continue_without_a_new_output_floor(monkeypatch, history_size):
    agent, current, frozen, sent, executed, recovery = _case(
        monkeypatch, history_size=history_size, cheap_continuation=True,
    )
    events = [event async for event in agent.run_turn(current)]
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert any(isinstance(event, DoneEvent) and FINAL in event.text for event in events)
    assert len(sent) == 3 and executed == ["read-once"]
    assert json.dumps(sent[-1][0]).count(PARTIAL) == 1
    if history_size == 20:
        assert not recovery
        assert [payload["max_tokens"] for payload, _, _ in sent] == [128_000] * 3
        assert json.dumps(sent[-1][0]).count("old log ") == 6
    assert agent.history_snapshot()[:len(frozen)] == frozen


@pytest.mark.asyncio
async def test_visible_length_without_removable_history_keeps_bounded_continuation(monkeypatch):
    agent, current, _frozen, sent, executed, _recovery = _case(monkeypatch, protected_only=True)
    events = [event async for event in agent.run_turn(current)]
    # One tool request, the first truncated response, and exactly the original
    # continuation allowance. Recovery may not drop the protected current input.
    assert len(sent) == 2 + agent.config.length_capped_continuations
    assert executed == ["read-once"]
    assert any(isinstance(event, ErrorEvent) and event.code == "provider_output_truncated"
               for event in events)
    assert all(current in json.dumps(payload) for payload, _, _ in sent)
    assert not any(isinstance(event, CompactionEvent) for event in events)
    assert len({cfg.turn_deadline_at_monotonic for _, cfg, _ in sent}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "deadline"])
async def test_visible_length_recovery_preserves_cancellation_and_parent_deadline(
    monkeypatch, failure,
):
    agent, current, frozen, sent, executed, _recovery = _case(
        monkeypatch, recovery_failure=failure,
    )
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            _ = [event async for event in agent.run_turn(current)]
    else:
        events = [event async for event in agent.run_turn(current)]
        # run_turn owns timeout presentation; the expired recovery propagates
        # to that existing terminal instead of starting another request.
        assert any(isinstance(event, ErrorEvent) and event.code == "agent_runtime_timeout"
                   for event in events)
        assert not any(isinstance(event, DoneEvent) and FINAL in event.text for event in events)
        assert not any(isinstance(event, CompactionEvent) for event in events)
    assert len(sent) == 2 and executed == ["read-once"]
    assert agent.history_snapshot()[:len(frozen)] == frozen
