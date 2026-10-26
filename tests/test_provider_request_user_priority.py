"""Emergency projection preserves task requests whenever the full envelope fits."""

from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest

from opensquilla.provider import request_proof
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import ChatConfig, DoneEvent, ErrorEvent, Message


def _config() -> ChatConfig:
    return ChatConfig(
        provider_context_window_tokens=32_768, max_tokens=8192,
        active_user_message_index=5,
    )


def _payload(*, oversized_user: bool = False) -> dict:
    task = (
        "Keep the original task context. " * 8
        + "The middle contains a concrete constraint that the continuation needs. "
        + "Preserve its exact value and scope. " * 10
    )
    if oversized_user:
        task += "Extra historical material. " * 5000
    return {
        "model": "qwen/qwen-2.5-72b-instruct", "max_tokens": 8192,
        "messages": [
            {"role": "system", "content": "Complete the task using the supplied history."},
            {"role": "user", "content": task},
            {"role": "assistant", "content": "Earlier verbose observations. " * 4000},
            {"role": "user", "content": "Unrelated older notes. " * 1500},
            {"role": "assistant", "content": "The latest completed reply stays intact."},
            {"role": "user", "content": "Continue the original task under its constraints."},
        ],
    }


@pytest.mark.parametrize("oversized_user", [False, True])
def test_emergency_checks_complete_users_before_lossy_fallback(monkeypatch, oversized_user):
    payload = _payload(oversized_user=oversized_user)
    original = deepcopy(payload)
    candidates = []
    compact = request_proof._emergency_compact_current_turn_payload_once

    def observe(candidate, **kwargs):
        result = compact(candidate, **kwargs)
        candidates.append((deepcopy(candidate), kwargs["preserve_user_content"], result))
        return result

    monkeypatch.setattr(request_proof, "_emergency_compact_current_turn_payload_once", observe)
    config = _config()
    projected, proof = request_proof.prove_or_compact_provider_payload(
        payload, projection_adapter="openrouter",
        proof_budget=request_proof.provider_request_character_budget(payload, config),
        token_budget=request_proof.provider_request_token_budget(payload, config),
        active_user_message_index=config.active_user_message_index,
    )
    assert proof and proof["fits"] and proof["compaction_tier"] == 3
    assert [preserve for _, preserve, _ in candidates] == (
        [True, False] if oversized_user else [True]
    )
    assert projected["messages"][5] == original["messages"][5]
    assert projected["messages"][4] == original["messages"][4]
    assert payload == original
    if oversized_user:
        assert candidates[0][0] == candidates[1][0]
        assert candidates[0][2]["messages"][2] == candidates[1][2]["messages"][2]
        assert projected["messages"][1] != original["messages"][1]
        assert projected == compact(candidates[1][0], active_user_message_index=5)
    else:
        assert projected["messages"][1] == original["messages"][1]
        assert projected["messages"][3] == original["messages"][3]


@pytest.mark.parametrize("preserve_user_content", [True, False])
def test_user_priority_keeps_active_media_and_protected_native_tool_pair(preserve_user_content):
    payload = _payload()
    media = {"type": "image", "source": {
        "type": "url", "url": "https://example.invalid/task.png",
    }}
    payload["messages"][5]["content"] = [
        {"type": "text", "text": "Active request " * 100}, media,
    ]
    payload["messages"].extend([
        {"role": "assistant", "content": [{
            "type": "tool_use", "id": "call-unresolved", "name": "check",
            "input": {"query": "Inspect the current result."},
        }]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "call-unresolved",
            "is_error": True, "content": "Preserve the exact failure. " * 500,
        }]},
    ])
    original = deepcopy(payload)
    projected = request_proof._emergency_compact_current_turn_payload_once(
        payload, active_user_message_index=5, protected_tool_result_indexes={0},
        preserve_user_content=preserve_user_content,
    )
    assert projected["messages"][5:] == original["messages"][5:]
    assert len(projected["messages"]) == len(original["messages"])
    assert payload == original


def test_historical_user_blocks_keep_text_while_old_tool_result_still_shrinks():
    task = "Historical user instructions with exact constraints. " * 20
    media = {"type": "image", "source": {
        "type": "url", "url": "https://example.invalid/historical.png",
    }}
    messages = []
    for index in range(3):
        messages.extend([
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": f"call-{index}", "name": "lookup",
                "input": {"query": "Inspect the recorded result."},
            }]},
            {"role": "user", "content": [
                *([{ "type": "text", "text": task}, media] if index == 0 else []),
                {"type": "tool_result", "tool_use_id": f"call-{index}",
                 "content": "Completed tool output. " * 300},
            ]},
        ])
    messages.append({"role": "user", "content": "Continue without losing task constraints."})
    payload = {"model": "qwen/qwen-2.5-72b-instruct", "max_tokens": 8192,
               "messages": messages}
    original = deepcopy(payload)
    projected = request_proof._emergency_compact_current_turn_payload_once(
        payload, active_user_message_index=6, preserve_user_content=True,
    )
    assert projected["messages"][1]["content"][:2] == original["messages"][1]["content"][:2]
    old_result = projected["messages"][1]["content"][2]
    assert old_result["tool_use_id"] == "call-0"
    assert "provider_request_tool_result_emergency_compacted" in old_result["content"]
    for index in (0, 2, 3, 4, 5, 6):
        assert projected["messages"][index] == original["messages"][index]
    config = _config().model_copy(update={"active_user_message_index": 6})
    proof = request_proof.prove_provider_payload(
        projected, projection_adapter="openrouter",
        proof_budget=request_proof.provider_request_character_budget(projected, config),
        token_budget=request_proof.provider_request_token_budget(projected, config),
        active_user_message_index=6,
    )
    assert proof["fits"]
    assert payload == original


@pytest.mark.asyncio
async def test_preserved_history_is_the_actual_adapter_request(monkeypatch):
    payload = _payload()
    original = deepcopy(payload)
    provider = OpenAIProvider(
        api_key="offline-only", model=payload["model"], provider_kind="openrouter",
    )
    messages = [Message.model_validate(message) for message in payload["messages"][1:]]
    frozen_messages = deepcopy(messages)
    config = _config().model_copy(update={
        "system": payload["messages"][0]["content"],
        "active_user_message_index": 4,
    })
    projection = provider.project_final_request(messages, config=config)
    assert not projection.fits
    expected, proof = request_proof.prove_or_compact_provider_payload(
        projection.payload, projection_adapter="openrouter",
        proof_budget=request_proof.provider_request_character_budget(projection.payload, config),
        token_budget=request_proof.provider_request_token_budget(projection.payload, config),
        active_user_message_index=projection.proof["active_user_message_index"],
    )
    assert proof and proof["fits"] and proof["compaction_tier"] == 3
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        body = {"choices": [{"index": 0, "delta": {"content": "Continuing."},
                             "finish_reason": "stop"}]}
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n".encode(),
        )

    client = httpx.AsyncClient
    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", lambda **kwargs: client(
        **{**kwargs, "transport": httpx.MockTransport(respond)},
    ))
    events = [event async for event in provider.chat(messages, config=config)]
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert any(isinstance(event, DoneEvent) for event in events)
    assert captured == [expected]
    for index in (1, 3, 5):
        assert captured[0]["messages"][index]["content"] == original["messages"][index]["content"]
    assert messages == frozen_messages
    assert payload == original
