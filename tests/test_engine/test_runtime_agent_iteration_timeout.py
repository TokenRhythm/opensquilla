from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from opensquilla.engine.agent import Agent
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import AgentConfig
from opensquilla.provider import DoneEvent, TextDeltaEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_timeout", [0.001, 444.0, -1.0])
@pytest.mark.parametrize("legacy_option", ["iteration_timeout", "tool_timeout"])
async def test_run_accepts_legacy_tool_and_iteration_timeouts_without_enforcing_them(
    monkeypatch: pytest.MonkeyPatch,
    legacy_timeout: float,
    legacy_option: str,
) -> None:
    """Legacy callers remain accepted, but bootstrap never arms the old watchdog."""
    from opensquilla.tools.types import ToolContext

    seen_kwargs: list[dict[str, Any]] = []
    real_agent_config = AgentConfig

    def recording_agent_config(**kwargs: Any) -> AgentConfig:
        seen_kwargs.append(kwargs)
        return real_agent_config(**kwargs)

    monkeypatch.setattr("opensquilla.engine.types.AgentConfig", recording_agent_config)

    provider = MagicMock()
    provider.provider_name = "stub"

    async def _chat(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield TextDeltaEvent(text="done")
        yield DoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)

    provider.chat = _chat

    selector = MagicMock()
    selector.resolve.return_value = provider
    selector.clone.return_value = selector
    selector.current_config = MagicMock(model="stub-model")

    session_manager = MagicMock()
    session_manager.get = AsyncMock(return_value=None)
    session_manager.append_message = AsyncMock(return_value=None)
    session_manager.update = AsyncMock(return_value=None)
    session_manager.get_compaction_summary = AsyncMock(return_value=None)
    session_manager.get_transcript = AsyncMock(return_value=[])

    runner = TurnRunner(
        provider_selector=selector,
        session_manager=session_manager,
    )

    tool_ctx = ToolContext(session_key="agent:main:iter-thread-test")

    events = [
        event
        async for event in runner.run(
            message="hi",
            session_key="agent:main:iter-thread-test",
            tool_context=tool_ctx,
            **{legacy_option: legacy_timeout},
        )
    ]

    assert not [event for event in events if event.kind == "error"]
    assert any(event.kind == "done" for event in events)

    assert seen_kwargs, "The turn must reach agent bootstrap"
    assert all(kw.get(legacy_option, 0.0) == 0.0 for kw in seen_kwargs)


@pytest.mark.asyncio
async def test_stream_total_timeout_does_not_double_close_provider_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent.__new__(Agent)
    agent.config = MagicMock(timeout=1.0, iteration_timeout=0.01)
    close_calls = 0

    async def provider_stream() -> AsyncIterator[dict[str, str]]:
        try:
            await asyncio.sleep(1.0)
            yield {"type": "chunk", "data": "late"}
        finally:
            await asyncio.sleep(0)

    async def record_close(_stream_iter: AsyncIterator[Any]) -> None:
        nonlocal close_calls
        close_calls += 1

    monkeypatch.setattr(agent, "_close_provider_stream", record_close)

    loop = asyncio.get_running_loop()

    with pytest.raises(TimeoutError, match="total timeout"):
        async for _event in agent._stream_provider_events_with_deadline(
            provider_stream(),
            loop=loop,
            total_deadline=loop.time() + 0.01,
        ):
            pass

    assert close_calls == 1


@pytest.mark.asyncio
async def test_legacy_iteration_timeout_does_not_stop_a_provider_response() -> None:
    agent = Agent.__new__(Agent)
    agent.config = AgentConfig(iteration_timeout=0.001)

    async def provider_stream() -> AsyncIterator[dict[str, str]]:
        await asyncio.sleep(0.02)
        yield {"type": "chunk", "data": "ready"}

    events = [
        event
        async for event in agent._stream_provider_events_with_deadline(
            provider_stream(),
            loop=asyncio.get_running_loop(),
            total_deadline=None,
        )
    ]

    assert events == [{"type": "chunk", "data": "ready"}]


@pytest.mark.asyncio
async def test_total_deadline_limited_wait_stays_total_timeout_on_early_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent.__new__(Agent)
    agent.config = SimpleNamespace(timeout=0.05, iteration_timeout=30.0)
    observed_timeouts: list[float | None] = []

    async def provider_stream() -> AsyncIterator[dict[str, str]]:
        await asyncio.sleep(30.0)
        yield {"type": "chunk", "data": "late"}

    async def return_before_deadline(
        futures: set[asyncio.Future[Any]],
        *,
        timeout: float | None = None,
    ) -> tuple[set[asyncio.Future[Any]], set[asyncio.Future[Any]]]:
        observed_timeouts.append(timeout)
        return set(), futures

    monkeypatch.setattr(asyncio, "wait", return_before_deadline)
    fake_loop: Any = SimpleNamespace(time=lambda: 10.0)

    with pytest.raises(TimeoutError, match="total timeout") as exc_info:
        async for _event in agent._stream_provider_events_with_deadline(
            provider_stream(),
            loop=fake_loop,
            total_deadline=10.05,
            deadline_provider=lambda: 10.10,
        ):
            pass

    assert type(exc_info.value) is TimeoutError
    assert getattr(
        exc_info.value,
        "_opensquilla_stream_deadline_at_monotonic",
    ) == pytest.approx(10.05)
    assert observed_timeouts == [pytest.approx(0.05)]
