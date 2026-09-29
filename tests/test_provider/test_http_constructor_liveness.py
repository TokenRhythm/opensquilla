"""The synchronous HTTPX constructor must not stop the Gateway event loop."""

from __future__ import annotations

import asyncio
import time

from opensquilla.provider import openai as openai_module
from opensquilla.provider.openai import OpenAIProvider


def test_http_client_constructor_is_offloaded(monkeypatch) -> None:
    provider = OpenAIProvider(
        api_key="synthetic-key",
        model="synthetic-model",
        base_url="https://provider.example.test/v1",
    )
    beats = 0

    def blocked_constructor(**_kwargs):
        time.sleep(0.25)
        return object()

    monkeypatch.setattr(openai_module.httpx, "AsyncClient", blocked_constructor)

    async def scenario() -> tuple[int, float]:
        nonlocal beats

        async def heartbeat() -> None:
            nonlocal beats
            while True:
                beats += 1
                await asyncio.sleep(0.01)

        heartbeat_task = asyncio.create_task(heartbeat())
        started = time.monotonic()
        client = await provider._build_http_client(timeout=1.0)
        elapsed = time.monotonic() - started
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        assert client is not None
        return beats, elapsed

    observed_beats, elapsed = asyncio.run(scenario())
    assert elapsed >= 0.20
    assert observed_beats >= 5
