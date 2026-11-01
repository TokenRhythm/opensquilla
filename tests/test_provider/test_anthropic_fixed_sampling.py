"""Fixed-sampling Claude models must not receive sampling controls.

Claude Opus 4.7+, Sonnet 5, and Fable 5 reject a non-default
``temperature`` with a 400 and accept only adaptive thinking; the
adapter used to forward both, so any configured temperature or
thinking level broke the first request on those models.
"""

from __future__ import annotations

import pytest

from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.types import ChatConfig, Message

_FIXED_SAMPLING_MODELS = [
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-fable-5",
    # Registry-style ids carry the vendor prefix.
    "anthropic/claude-opus-4-8",
]


def _payload(model: str, cfg: ChatConfig) -> dict:
    provider = AnthropicProvider(api_key="sk-test", model=model)
    projection = provider.project_final_request(
        [Message(role="user", content="Say hello.")], config=cfg
    )
    return projection.payload


@pytest.mark.parametrize("model", _FIXED_SAMPLING_MODELS)
def test_temperature_is_omitted_for_fixed_sampling_models(model: str) -> None:
    payload = _payload(model, ChatConfig(temperature=0.7))
    assert "temperature" not in payload


@pytest.mark.parametrize("model", _FIXED_SAMPLING_MODELS)
def test_thinking_is_adaptive_for_fixed_sampling_models(model: str) -> None:
    payload = _payload(model, ChatConfig(thinking=True))
    assert payload["thinking"] == {"type": "adaptive"}


def test_sonnet_4_6_keeps_temperature_and_adaptive_thinking() -> None:
    payload = _payload("claude-sonnet-4-6", ChatConfig(temperature=0.7))
    assert payload["temperature"] == 0.7
    payload = _payload("claude-sonnet-4-6", ChatConfig(thinking=True))
    assert payload["thinking"] == {"type": "adaptive"}


def test_sonnet_4_5_keeps_temperature_and_budget_thinking() -> None:
    payload = _payload("claude-sonnet-4-5", ChatConfig(temperature=0.7))
    assert payload["temperature"] == 0.7
    payload = _payload("claude-sonnet-4-5", ChatConfig(thinking=True))
    assert payload["thinking"]["type"] == "enabled"
    assert payload["thinking"]["budget_tokens"] == 5000


def test_haiku_4_5_keeps_temperature() -> None:
    payload = _payload("claude-haiku-4-5-20251001", ChatConfig(temperature=0.7))
    assert payload["temperature"] == 0.7
