"""Opt-in live LLM smoke.

This is intentionally the only live LLM smoke restored for open-source
readiness. It skips unless credentials are explicitly present.
"""

from __future__ import annotations

import os
import uuid

import pytest

from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ProviderRequestCorrelation,
    TextDeltaEvent,
)

pytestmark = [pytest.mark.llm, pytest.mark.llm_smoke]

_EXPECTED_TOKEN = "opensquilla-live-smoke-ok"


@pytest.mark.asyncio
async def test_openrouter_live_smoke_returns_expected_token() -> None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        pytest.skip("OPENROUTER_API_KEY not set")

    provider = OpenAIProvider(
        api_key=api_key,
        model=os.environ.get("LLM_TEST_MODEL", "openai/gpt-4o-mini"),
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        provider_kind="openrouter",
    )
    text_parts: list[str] = []
    done = False

    async for event in provider.chat(
        [Message(role="user", content=f"Reply with exactly {_EXPECTED_TOKEN}.")],
        config=ChatConfig(max_tokens=32, temperature=0.0, timeout=45.0),
    ):
        if isinstance(event, ErrorEvent):
            pytest.fail(f"live LLM smoke failed: {event.code} {event.message}")
        if isinstance(event, TextDeltaEvent):
            text_parts.append(event.text)
        if isinstance(event, DoneEvent):
            done = True

    assert done is True
    assert _EXPECTED_TOKEN in "".join(text_parts).strip().lower()


@pytest.mark.asyncio
async def test_tokenrhythm_live_smoke_returns_expected_token() -> None:
    api_key = os.environ.get("TOKENRHYTHM_API_KEY")
    if not api_key:
        pytest.skip("TOKENRHYTHM_API_KEY not set")

    provider = OpenAIProvider(
        api_key=api_key,
        model=os.environ.get("TOKENRHYTHM_MODEL", "deepseek-v4-flash"),
        base_url=os.environ.get("TOKENRHYTHM_BASE_URL", "https://tokenrhythm.studio/v1"),
        provider_kind="tokenrhythm",
    )
    root = uuid.uuid4().hex
    configs = (
        ChatConfig(max_tokens=1024, temperature=0.0, timeout=90.0),
        ChatConfig(
            max_tokens=1024,
            temperature=0.0,
            timeout=90.0,
            provider_request_correlation=ProviderRequestCorrelation(
                session_id=f"live-session-{root}",
                turn_id=f"live-turn-{root}",
                execution_id=f"live-execution-{root}",
                call_kind="agent.chat",
            ),
        ),
    )

    for config in configs:
        text_parts: list[str] = []
        done_event: DoneEvent | None = None
        async for event in provider.chat(
            [Message(role="user", content=f"Reply with exactly {_EXPECTED_TOKEN}.")],
            # Every TokenRhythm model spends reasoning_content tokens out of
            # max_tokens before any text; a small budget returns empty content
            # with finish_reason "length".
            config=config,
        ):
            if isinstance(event, ErrorEvent):
                pytest.fail(f"live LLM smoke failed: {event.code} {event.message}")
            if isinstance(event, TextDeltaEvent):
                text_parts.append(event.text)
            if isinstance(event, DoneEvent):
                done_event = event

        assert done_event is not None
        assert done_event.input_tokens >= 0
        assert done_event.output_tokens > 0
        assert _EXPECTED_TOKEN in "".join(text_parts).strip().lower()
