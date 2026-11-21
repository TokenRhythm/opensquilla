"""Sampling-parameter projection for the Anthropic-compatible request payload.

Anthropic's Messages API rejects requests that combine extended thinking
with temperature or top_p, so the client must omit those fields while a
thinking payload is active (issue 1431: a hosted Anthropic-compatible
surface returning 400 for the combination is contract-conformant).
"""

from __future__ import annotations

from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.types import ChatConfig, Message


def _payload(**config) -> dict:
    provider = AnthropicProvider(api_key="test", model="claude-test")
    payload, _ = provider._build_payload(
        [Message(role="user", content="hello")],
        None,
        ChatConfig(**config),
        record_diagnostics=False,
    )
    return payload


def test_thinking_request_omits_temperature_and_top_p() -> None:
    payload = _payload(thinking=True, temperature=0.7, top_p=0.9)

    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 5000}
    assert "temperature" not in payload
    assert "top_p" not in payload


def test_plain_request_keeps_requested_temperature() -> None:
    payload = _payload(temperature=0.7, top_p=0.9)

    assert "thinking" not in payload
    assert payload["temperature"] == 0.7
    # The client never projects top_p onto this surface; the same
    # contract keeps it absent when thinking is enabled.
    assert "top_p" not in payload


def test_thinking_request_without_sampling_params_stays_clean() -> None:
    payload = _payload(thinking=True)

    assert "temperature" not in payload
    assert "top_p" not in payload
    assert "thinking" in payload
