"""Structured refusals remain terminal usage receipts and ordinary chat text."""

from __future__ import annotations

import json

import httpx
import pytest

from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import ChatConfig, DoneEvent, Message, TextDeltaEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("refusal", [None, "Synthetic refusal"])
async def test_openai_preserves_refusal_evidence_without_changing_chat(
    monkeypatch, streaming, refusal,
):
    message = {"content": "Visible response", "refusal": refusal}
    usage = {"prompt_tokens": 10, "completion_tokens": 3}

    def respond(request):
        if not streaming:
            return httpx.Response(200, json={
                "model": "synthetic-model",
                "choices": [{"message": message, "finish_reason": "stop"}],
                "usage": usage,
            })
        frames = [
            {"choices": [{"index": 0, "delta": message, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": usage},
        ]
        body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text=body + "data: [DONE]\n\n")

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )
    provider = OpenAIProvider(
        api_key="synthetic-key", model="synthetic-model",
        base_url="https://provider.example.test/v1",
    )
    config = ChatConfig(max_tokens=32)
    if streaming:
        stream = provider.chat([Message(role="user", content="Synthetic request")], config=config)
    else:
        stream = provider._complete_non_stream(
            payload={"model": "synthetic-model", "messages": [], "stream": True},
            headers={"Authorization": "Bearer synthetic-key"},
            cfg=config, tools=None, timeout_exc=httpx.ReadTimeout("Synthetic timeout"),
        )
    events = [event async for event in stream]
    assert "".join(event.text for event in events if isinstance(event, TextDeltaEvent)) == (
        "Visible response"
    )
    done, = [event for event in events if isinstance(event, DoneEvent)]
    assert done.refusal is bool(refusal)
    assert done.stop_reason == "stop"
    assert (done.input_tokens, done.output_tokens) == (10, 3)
