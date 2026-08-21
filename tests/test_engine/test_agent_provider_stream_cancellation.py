import asyncio
from types import SimpleNamespace

import pytest

import opensquilla.engine.agent as agent_module


class _BlockingProviderStream:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    def __aiter__(self) -> "_BlockingProviderStream":
        return self

    async def __anext__(self) -> object:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise StopAsyncIteration


class _StubbornProviderStream:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_calls = 0

    def __aiter__(self) -> "_StubbornProviderStream":
        return self

    async def __anext__(self) -> object:
        self.started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_calls += 1
        self.closed.set()


@pytest.mark.asyncio
async def test_provider_next_event_is_cancelled_when_turn_stream_is_cancelled() -> None:
    agent = agent_module.Agent.__new__(agent_module.Agent)
    agent.config = SimpleNamespace(iteration_timeout=60.0, timeout=60.0)
    stream = _BlockingProviderStream()

    async def consume() -> None:
        async for _event in agent_module.Agent._stream_provider_events_with_deadline(
            agent,
            stream,
            loop=asyncio.get_running_loop(),
            total_deadline=None,
        ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(stream.started.wait(), timeout=0.25)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    try:
        await asyncio.wait_for(stream.cancelled.wait(), timeout=0.25)
    finally:
        stream.release.set()


@pytest.mark.asyncio
async def test_stubborn_provider_timeout_is_bounded_and_close_is_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_module, "TIMEOUT_CANCEL_GRACE_SECONDS", 0.02)
    agent = agent_module.Agent.__new__(agent_module.Agent)
    agent.config = SimpleNamespace(iteration_timeout=0.01, timeout=1.0)
    stream = _StubbornProviderStream()

    async def consume() -> None:
        async for _event in agent._stream_provider_events_with_deadline(
            stream,
            loop=asyncio.get_running_loop(),
            total_deadline=None,
        ):
            pass

    with pytest.raises(agent_module._IterationStreamTimeoutError):
        await asyncio.wait_for(consume(), timeout=0.2)

    assert stream.cancelled.is_set()
    assert not stream.closed.is_set()

    stream.release.set()
    await asyncio.wait_for(stream.closed.wait(), timeout=0.2)
    assert stream.close_calls == 1


@pytest.mark.asyncio
async def test_stubborn_provider_stop_uses_short_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_module, "STOP_CANCEL_GRACE_SECONDS", 0.02)
    agent = agent_module.Agent.__new__(agent_module.Agent)
    agent.config = SimpleNamespace(iteration_timeout=60.0, timeout=60.0)
    stream = _StubbornProviderStream()

    async def consume() -> None:
        async for _event in agent._stream_provider_events_with_deadline(
            stream,
            loop=asyncio.get_running_loop(),
            total_deadline=None,
        ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(stream.started.wait(), timeout=0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.2)

    assert stream.cancelled.is_set()
    assert not stream.closed.is_set()
    stream.release.set()
    await asyncio.wait_for(stream.closed.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_stubborn_provider_total_deadline_uses_timeout_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_module, "TIMEOUT_CANCEL_GRACE_SECONDS", 0.02)
    agent = agent_module.Agent.__new__(agent_module.Agent)
    agent.config = SimpleNamespace(iteration_timeout=60.0, timeout=0.01)
    stream = _StubbornProviderStream()
    loop = asyncio.get_running_loop()

    async def consume() -> None:
        async for _event in agent._stream_provider_events_with_deadline(
            stream,
            loop=loop,
            total_deadline=loop.time() + 0.01,
        ):
            pass

    with pytest.raises(TimeoutError, match="total timeout"):
        await asyncio.wait_for(consume(), timeout=0.2)

    assert stream.cancelled.is_set()
    assert not stream.closed.is_set()
    stream.release.set()
    await asyncio.wait_for(stream.closed.wait(), timeout=0.2)
