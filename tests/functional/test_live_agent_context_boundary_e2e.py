"""Opt-in live agent context-boundary smoke.

This test uses public synthetic text only and skips unless explicitly enabled
with local credentials.
"""

from __future__ import annotations

import os

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.provider import Message
from opensquilla.provider.openai import OpenAIProvider

pytestmark = [pytest.mark.llm, pytest.mark.llm_smoke, pytest.mark.agent_context_boundary]

_EXPECTED_TOKEN = "opensquilla-agent-boundary-live-ok"
_INTERNAL_MARKERS = (
    "opensquilla_compacted",
    "tool_use_argument_projection",
    "provider_request_compacted",
    "invalid_provider_context_projection",
)


@pytest.mark.asyncio
async def test_live_openrouter_agent_boundary_smoke() -> None:
    if os.environ.get("OPENSQUILLA_AGENT_CONTEXT_BOUNDARY_LIVE") != "1":
        pytest.skip("set OPENSQUILLA_AGENT_CONTEXT_BOUNDARY_LIVE=1 to run live smoke")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        pytest.skip("OPENROUTER_API_KEY not set")

    provider = OpenAIProvider(
        api_key=api_key,
        model=os.environ.get("LLM_TEST_MODEL", "openai/gpt-4o-mini"),
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        provider_kind="openrouter",
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=200_000,
            max_tokens=64,
            provider_request_proof_max_chars=8_000,

            max_iterations=1,
            request_timeout=45.0,
        ),
    )
    agent.set_history(
        [
            Message(
                role="user",
                content="Public dummy archive context.\n" + ("alpha beta gamma\n" * 2000),
            ),
            Message(
                role="assistant",
                content="Public dummy previous answer.\n" + ("delta epsilon\n" * 2000),
            ),
        ]
    )

    events = [
        event
        async for event in agent.run_turn(f"Reply with exactly {_EXPECTED_TOKEN}.")
    ]
    text = "\n".join(str(getattr(event, "text", "")) for event in events).lower()

    assert _EXPECTED_TOKEN in text
    assert not any(marker in text for marker in _INTERNAL_MARKERS)
