from __future__ import annotations

import asyncio
from typing import Any

import pytest

from opensquilla.engine.context_budget import ContextBudgetDecision
from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.ollama import OllamaProvider
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.openai_codex import OpenAICodexProvider
from opensquilla.provider.openai_responses import OpenAIResponsesProvider
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockText,
    ContentBlockToolUse,
    Message,
)


def _capture_final_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    captured: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def coordinate(
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> ContextBudgetDecision:
        captured.append((payload, kwargs))
        return ContextBudgetDecision(
            action="budget_limited",
            payload=None,
            proof={"fits": False},
            reason="test_capture",
        )

    monkeypatch.setattr(
        "opensquilla.engine.context_budget.coordinate_provider_context_budget",
        coordinate,
    )
    return captured


def _collect(provider: Any, messages: list[Message], config: ChatConfig) -> list[Any]:
    async def run() -> list[Any]:
        return [event async for event in provider.chat(messages, config=config)]

    return asyncio.run(run())


def _messages_with_system_shift() -> list[Message]:
    return [
        Message(role="assistant", content="prior answer"),
        Message(role="user", content="ACTIVE USER PROMPT"),
        Message(role="user", content="synthetic candidate bundle"),
    ]


def _messages_with_responses_expansion() -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[
                ContentBlockText(text="prior answer"),
                ContentBlockToolUse(id="call-1", name="read_file", input={"path": "x"}),
            ],
        ),
        Message(role="user", content="ACTIVE USER PROMPT"),
        Message(role="user", content="synthetic candidate bundle"),
    ]


def test_openai_final_proof_uses_system_shifted_active_user_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_final_proof(monkeypatch)
    provider = OpenAIProvider(api_key="test", model="gpt-test")

    _collect(
        provider,
        _messages_with_system_shift(),
        ChatConfig(system="authoritative system", active_user_message_index=1),
    )

    payload, proof_kwargs = captured[0]
    assert proof_kwargs["active_user_message_index"] == 2
    assert payload["messages"][2] == {
        "role": "user",
        "content": "ACTIVE USER PROMPT",
    }


def test_ollama_final_proof_uses_system_shifted_active_user_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_final_proof(monkeypatch)
    provider = OllamaProvider(model="test-model")

    _collect(
        provider,
        _messages_with_system_shift(),
        ChatConfig(system="authoritative system", active_user_message_index=1),
    )

    payload, proof_kwargs = captured[0]
    assert proof_kwargs["active_user_message_index"] == 2
    assert payload["messages"][2] == {
        "role": "user",
        "content": "ACTIVE USER PROMPT",
    }


def test_responses_final_proof_uses_expanded_wire_item_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_final_proof(monkeypatch)
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-test")

    _collect(
        provider,
        _messages_with_responses_expansion(),
        ChatConfig(active_user_message_index=1),
    )

    payload, proof_kwargs = captured[0]
    assert proof_kwargs["active_user_message_index"] == 2
    assert payload["input"][2] == {
        "role": "user",
        "content": "ACTIVE USER PROMPT",
    }


def test_codex_final_proof_uses_expanded_wire_item_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_final_proof(monkeypatch)
    monkeypatch.setattr(
        "opensquilla.provider.openai_codex.load_codex_credentials",
        lambda _path: object(),
    )
    provider = OpenAICodexProvider(model="gpt-test")

    _collect(
        provider,
        _messages_with_responses_expansion(),
        ChatConfig(active_user_message_index=1),
    )

    payload, proof_kwargs = captured[0]
    assert proof_kwargs["active_user_message_index"] == 2
    assert payload["input"][2] == {
        "role": "user",
        "content": "ACTIVE USER PROMPT",
    }


def test_anthropic_final_proof_preserves_one_to_one_active_user_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_final_proof(monkeypatch)
    provider = AnthropicProvider(api_key="test", model="claude-test")

    _collect(
        provider,
        _messages_with_system_shift(),
        ChatConfig(system="authoritative system", active_user_message_index=1),
    )

    payload, proof_kwargs = captured[0]
    assert proof_kwargs["active_user_message_index"] == 1
    assert payload["messages"][1] == {
        "role": "user",
        "content": "ACTIVE USER PROMPT",
    }
