from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import httpx
import pytest

from opensquilla.context_budget import hard_input_tokens, soft_input_tokens
from opensquilla.provider import openai as openai_module
from opensquilla.provider import request_proof
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.request_proof import (
    effective_proof_token_budget,
    fit_generation_tokens,
    fit_payload_generation_budget,
)
from opensquilla.provider.types import ChatConfig, DoneEvent, Message


@pytest.mark.parametrize("window,requested,input_tokens", [
    (32_768, 8_192, 26_000),
    (128_000, 32_768, 100_000),
    (200_000, 128_000, 68_277),
    (1_000_000, 131_072, 880_000),
])
def test_generation_cap_uses_remaining_capacity_once(
    window: int, requested: int, input_tokens: int,
) -> None:
    cap = fit_generation_tokens(window, requested, input_tokens)
    assert 1024 <= cap < requested
    assert effective_proof_token_budget(hard_input_tokens(window, cap))[0] >= input_tokens
    assert effective_proof_token_budget(hard_input_tokens(window, cap + 1))[0] < input_tokens
    assert soft_input_tokens(window) > hard_input_tokens(window, requested)


def test_small_explicit_cap_is_never_raised_and_physical_overflow_still_rejected() -> None:
    assert fit_generation_tokens(200_000, 256, 10_000) == 256
    assert fit_generation_tokens(200_000, 128_000, 199_000) == 0
    assert fit_generation_tokens(0, 128_000, 999_999) == 128_000
    assert soft_input_tokens(200_000) == 180_000


def test_legal_short_answer_is_not_blocked_by_an_invented_output_floor(monkeypatch: Any) -> None:
    monkeypatch.setattr(request_proof, "_serialized_token_estimate", lambda _: (29_000, "test"))
    provider = OpenAIProvider(api_key="test", model="synthetic")
    config = ChatConfig(max_tokens=4096, thinking=False, provider_context_window_tokens=32_768)
    messages = [Message(role="user", content="Reply OK.")]
    projection = provider.project_final_request(messages, config=config)
    assert projection.fits
    assert projection.proof["effective_generation_tokens"] == 546
    assert projection.payload["messages"][-1]["content"] == "Reply OK."
    assert config.max_tokens == 4096


def test_payload_estimate_includes_tools_system_and_media(monkeypatch: Any) -> None:
    seen: list[dict[str, Any]] = []

    def estimate(serialized: str) -> tuple[int, str]:
        seen.append(json.loads(serialized))
        return 26_000, "controlled_input"

    monkeypatch.setattr(request_proof, "_serialized_token_estimate", estimate)
    payload = {
        "model": "synthetic", "max_tokens": 16_384,
        "messages": [
            {"role": "system", "content": "fixed instructions"},
            {"role": "user", "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}},
            ]},
        ],
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
    }
    original = deepcopy(payload)
    config = ChatConfig(max_tokens=16_384, provider_context_window_tokens=32_768)
    fitted, proof = fit_payload_generation_budget(payload, config)
    assert payload == original
    assert seen[0]["tools"] == payload["tools"]
    assert seen[0]["messages"][0] == payload["messages"][0]
    assert fitted["max_tokens"] < fit_generation_tokens(32_768, 16_384, 26_000)
    assert proof["requested_generation_tokens"] == 16_384
    assert proof["effective_generation_tokens"] == fitted["max_tokens"]


@pytest.mark.parametrize("field", ["thinking_budget", "thinking", "reasoning"])
def test_wire_reasoning_budget_is_not_silently_cut(monkeypatch: Any, field: str) -> None:
    monkeypatch.setattr(request_proof, "_serialized_token_estimate", lambda _: (26_000, "test"))
    payload = {"max_tokens": 16_384, "messages": [{"role": "user", "content": "x"}]}
    payload[field] = {
        "thinking_budget": 5_000,
        "thinking": {"type": "enabled", "budget_tokens": 5_000},
        "reasoning": {"max_tokens": 5_000},
    }[field]
    fitted, proof = fit_payload_generation_budget(
        payload, ChatConfig(max_tokens=16_384, provider_context_window_tokens=32_768),
    )
    assert fitted == payload
    assert not proof["generation_budget_adjusted"]


def test_unknown_window_and_summary_goal_remain_unchanged(monkeypatch: Any) -> None:
    monkeypatch.setattr(request_proof, "_serialized_token_estimate", lambda _: (10_000, "test"))
    payload = {"max_tokens": 8192, "messages": [{"role": "user", "content": "summarize"}]}
    for config in (ChatConfig(), ChatConfig(provider_context_window_tokens=200_000)):
        fitted, proof = fit_payload_generation_budget(payload, config)
        assert fitted == payload
        assert proof["effective_generation_tokens"] == 8192


def test_ascii_heavy_input_also_fits_derived_character_guard(monkeypatch: Any) -> None:
    monkeypatch.setattr(request_proof, "_serialized_token_estimate", lambda _: (80_000, "test"))
    provider = OpenAIProvider(api_key="test", model="glm-5.1")
    projection = provider.project_final_request(
        [Message(role="user", content="item " * 80_000)],
        config=ChatConfig(
            max_tokens=128_000, provider_context_window_tokens=200_000,
            provider_request_max_chars_explicit_cap=0,
        ),
    )
    assert projection.fits
    assert projection.proof["fits_char_budget"]
    assert projection.proof["fits_token_budget"]
    assert 90_000 < projection.proof["effective_generation_tokens"] < 100_000


@pytest.mark.parametrize("explicit_chars", [0, 1000])
def test_large_glm_request_projection_matches_wire_without_changing_input(
    monkeypatch: Any, explicit_chars: int,
) -> None:
    monkeypatch.setattr(
        request_proof, "_serialized_token_estimate",
        lambda serialized: ((len(serialized) + 3) // 4, "controlled_input"),
    )
    payloads: list[dict[str, Any]] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        body = {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n".encode(),
        )

    monkeypatch.setattr(
        openai_module.httpx, "AsyncClient",
        lambda **kwargs: real_client(**{**kwargs, "transport": httpx.MockTransport(handler)}),
    )
    provider = OpenAIProvider(api_key="test", model="glm-5.1")
    config = ChatConfig(
        max_tokens=128_000, provider_context_window_tokens=200_000,
        provider_request_max_chars_explicit_cap=explicit_chars,
        physical_attempt_limit=1,
    )
    messages = [Message(role="user", content="x" * 272_000)]
    projection = provider.project_final_request(messages, config=config)
    assert projection.proof["requested_generation_tokens"] == 128_000
    assert 120_000 < projection.proof["effective_generation_tokens"] < 128_000
    assert projection.fits is (explicit_chars == 0)

    async def run() -> list[Any]:
        return [event async for event in provider.chat(messages, config=config)]

    events = asyncio.run(run())
    if explicit_chars:
        assert payloads == []
    else:
        assert any(isinstance(event, DoneEvent) for event in events)
        assert payloads == [projection.payload]
        assert payloads[0]["messages"][-1]["content"] == messages[0].content
        assert config.max_tokens == 128_000
