"""Capture actual adapter sends when remaining context limits generation."""

from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig, DoneEvent, ErrorEvent, ToolResult
from opensquilla.provider import (
    Message,
    ModelCapabilities,
    OpenAIProvider,
    ToolDefinition,
    request_proof,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "large_history,large_current", [(False, False), (True, False), (True, True)]
)
async def test_reasoning_length_retry_frees_input_only_when_output_was_capacity_limited(
    monkeypatch,
    large_history,
    large_current,
):
    monkeypatch.setattr(
        request_proof,
        "_serialized_token_estimate",
        lambda text: ((len(text) + 3) // 4, "controlled_full_payload"),
    )
    provider = OpenAIProvider(
        api_key="unused",
        model="glm-5.1",
        provider_kind="tokenrhythm",
        base_url="https://tokenrhythm.studio/v1",
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_id="glm-5.1",
            provider_id="tokenrhythm",
            context_window_tokens=200_000,
            context_window_known=True,
            max_tokens=128_000,
            thinking=True,
            model_capabilities=ModelCapabilities(supports_tools=True, supports_reasoning=True),
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            timeout=30,
            iteration_timeout=30,
        ),
    )
    history = []
    for index in range(6):
        history.extend(
            [
                Message(role="user", content=f"Historical request {index}"),
                Message(
                    role="assistant", content="old log " + "x" * (60_000 if large_history else 20)
                ),
            ]
        )
    agent.set_history(history)
    frozen = deepcopy(history)
    sent = []
    real_client = httpx.AsyncClient

    def handle(request):
        payload = json.loads(request.content)
        sent.append(payload)
        cap = payload.get("max_completion_tokens", payload.get("max_tokens"))
        if len(sent) == 1:
            delta, stop, output = (
                {"reasoning_content": "Planning the remaining work."},
                "length",
                cap,
            )
        else:
            delta, stop, output = (
                {"content": "Continued: display all 100 cards without filtering."},
                "stop",
                12,
            )
        chunk = {
            "choices": [{"delta": delta, "finish_reason": stop}],
            "usage": {
                "prompt_tokens": 90000 if large_history else 500,
                "completion_tokens": output,
                "completion_tokens_details": {"reasoning_tokens": output if len(sent) == 1 else 0},
            },
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode(),
        )

    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        lambda **kw: real_client(**{**kw, "transport": httpx.MockTransport(handle)}),
    )
    current = "Display all 100 game cards immediately. Do not require a search."
    if large_current:
        current += "\nRequired current material: " + "y" * 280_000
    events = [event async for event in agent.run_turn(current)]
    assert len(sent) == 2
    caps = [p.get("max_completion_tokens", p.get("max_tokens")) for p in sent]
    if large_history:
        assert caps[0] < caps[1] <= 128_000
        if large_current:
            # A full restoration is impossible with this protected current input.
            assert caps[1] < 128_000
        assert len(json.dumps(sent[1])) < len(json.dumps(sent[0]))
    else:
        assert caps == [128_000, 128_000]
    assert any(
        current in m.get("content", "")
        for m in sent[1]["messages"]
        if isinstance(m.get("content"), str)
    )
    assert agent.config.thinking is True
    assert agent.config.max_tokens == 128_000
    assert agent.history_snapshot()[: len(history)] == frozen
    assert any(isinstance(e, DoneEvent) and "100 cards" in e.text for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)


@pytest.mark.asyncio
async def test_large_completed_read_reaches_next_request_without_read_again(monkeypatch):
    monkeypatch.setattr(
        request_proof,
        "_serialized_token_estimate",
        lambda text: ((len(text) + 3) // 4, "controlled_full_payload"),
    )
    provider = OpenAIProvider(
        api_key="unused",
        model="glm-5.1",
        provider_kind="tokenrhythm",
        base_url="https://tokenrhythm.studio/v1",
    )
    body = "HEAD-100-GAMES\n" + "x" * 272_000 + "\nALL-CARDS-RENDER-AT-START"
    reads = []
    sent = []

    async def read(call):
        reads.append(call.tool_use_id)
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content=body)

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_id="glm-5.1",
            provider_id="tokenrhythm",
            context_window_tokens=200_000,
            context_window_known=True,
            max_tokens=128_000,
            model_capabilities=ModelCapabilities(supports_tools=True),
            tool_result_compression_enabled=False,
            tool_result_projection_max_inline_chars=2_000_000,
            timeout=30,
            iteration_timeout=30,
        ),
        tool_definitions=[
            ToolDefinition(name="read_file", description="Read a file", input_schema={})
        ],
        tool_handler=read,
    )
    real_client = httpx.AsyncClient

    def handle(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if len(sent) == 1:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "read-once",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"100games.html"}'},
                    }
                ]
            }
            stop = "tool_calls"
        else:
            # The lost-result failure would request the same file again here.
            assert body in [m.get("content") for m in payload["messages"]]
            delta, stop = {"content": "Verified all 100 cards render on opening."}, "stop"
        chunk = {"choices": [{"delta": delta, "finish_reason": stop}]}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode(),
        )

    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        lambda **kw: real_client(**{**kw, "transport": httpx.MockTransport(handle)}),
    )
    events = [event async for event in agent.run_turn("Check that all games display immediately.")]
    assert reads == ["read-once"]
    assert len(sent) == 2
    assert sent[0]["max_tokens"] == 128_000
    assert 120_000 < sent[1]["max_tokens"] < 128_000
    assert any(isinstance(e, DoneEvent) and "100 cards" in e.text for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)
