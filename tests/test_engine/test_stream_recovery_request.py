from __future__ import annotations

import copy

import httpx
import pytest
from test_selector_provider_recovery import _Provider, _wrapper

from opensquilla.contracts.turn_execution import TurnExecutionContext, TurnIdentity
from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import _selector_stream_exception_error
from opensquilla.engine.stream_recovery import StreamRecoveryRequest
from opensquilla.provider.types import ChatConfig, DoneEvent, ErrorEvent, Message, TextDeltaEvent


@pytest.mark.parametrize(
    ("error", "origin"),
    [
        (httpx.ReadTimeout("private upstream prose"), "read_timeout"),
        (httpx.ReadError("private upstream prose"), "transport_disconnect"),
        (TypeError("timeout in local parser"), "unknown"),
    ],
)
def test_selector_preserves_typed_transport_evidence(error, origin):
    projected = _selector_stream_exception_error(content_started=True, error=error)
    assert projected.code == "response_incomplete"
    assert projected.recovery_origin == origin
    assert "private upstream prose" not in projected.message


async def test_recovery_freezes_model_input_but_allocates_new_dispatches():
    class Provider:
        provider_name = "synthetic-provider"

        def __init__(self):
            self.calls = []

        async def chat(self, messages, tools=None, config=None):
            self.calls.append((copy.deepcopy(messages), config))
            messages[0].content = "adapter mutated its own input"
            yield DoneEvent(stop_reason="stop")

    provider = Provider()
    messages = [Message(role="user", content="admitted input")]
    config = ChatConfig(timeout=120, max_tokens=17)
    request = StreamRecoveryRequest.capture(provider, messages, None, config)
    assert request is not None
    messages[0].content = "later steering"
    config.max_tokens = 999

    for _ in range(2):
        _events = [event async for event in request.open(None)]

    assert [call[0][0].content for call in provider.calls] == ["admitted input"] * 2
    assert all(call[1].physical_attempt_limit == 1 for call in provider.calls)
    assert all(call[1].timeout == 120 for call in provider.calls)
    assert all(call[1].max_tokens == 17 for call in provider.calls)
    assert provider.calls[0][1] is not provider.calls[1][1]


async def test_selector_recovery_uses_actual_projected_leg_without_rerouting(monkeypatch):
    dispatched = []
    chat = _Provider.chat

    def record_dispatch(self, messages, tools=None, config=None):
        dispatched.append((self.model, copy.deepcopy(messages), config))
        return chat(self, messages, tools=tools, config=config)

    monkeypatch.setattr(_Provider, "chat", record_dispatch)
    wrapper, _health, calls = _wrapper(monkeypatch, {
        "primary": [[ErrorEvent(code="404", message="model not found")]],
        "secondary": [
            [TextDeltaEvent(text="discard"), ErrorEvent(
                code="timeout", recovery_origin="read_timeout",
            )],
            [TextDeltaEvent(text="accepted"), DoneEvent(stop_reason="stop")],
        ],
        "third": [[TextDeltaEvent(text="wrong deployment"), DoneEvent(stop_reason="stop")]],
    })
    context = TurnExecutionContext(TurnIdentity("turn", "answer", "synthetic-session"))
    agent = Agent(wrapper, AgentConfig(
        retry_base_backoff_ms=0, retry_max_backoff_ms=0,
    ), execution_context=context)
    events = [event async for event in agent.run_turn("synthetic request")]

    assert calls == ["primary", "secondary", "secondary"]
    assert dispatched[1][1] == dispatched[2][1]
    assert dispatched[2][2].physical_attempt_limit == 1
    assert [event.text for event in events if event.kind == "done"] == ["accepted"]
    reset = next(event for event in events if event.kind == "answer_generation_reset")
    assert reset.reset_scope == "provider_call"
    assert reset.rollback_model_call_id == "1.0"
