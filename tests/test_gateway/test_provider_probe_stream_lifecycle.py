from __future__ import annotations

import asyncio
from typing import Any

import pytest

import opensquilla.engine.usage_accounting as usage_accounting
import opensquilla.onboarding.probe as probe_module
from opensquilla.engine.usage_accounting import UsageCallResult, UsageCallStart
from opensquilla.gateway.rpc_onboarding import _usage_accounted_provider_probe
from opensquilla.provider.types import DoneEvent


class _RecordingSink:
    def __init__(self) -> None:
        self.started: list[UsageCallStart] = []
        self.finalized: list[tuple[UsageCallStart, UsageCallResult]] = []
        self.unknown: list[tuple[UsageCallStart, str]] = []

    async def start(self, call: UsageCallStart) -> None:
        self.started.append(call)

    async def finalize(self, call: UsageCallStart, result: UsageCallResult) -> None:
        self.finalized.append((call, result))

    async def mark_unknown(self, call: UsageCallStart, reason: str) -> None:
        self.unknown.append((call, reason))


class _DoneThenOpenStream:
    def __init__(self) -> None:
        self._sent_done = False
        self.close_calls = 0

    def __aiter__(self) -> _DoneThenOpenStream:
        return self

    async def __anext__(self) -> DoneEvent:
        if self._sent_done:
            raise AssertionError("the probe must stop after the terminal event")
        self._sent_done = True
        return DoneEvent(input_tokens=1, output_tokens=1, model="gpt-test")

    async def aclose(self) -> None:
        self.close_calls += 1


class _CancellationResistantCloseStream(_DoneThenOpenStream):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.close_cancelled = asyncio.Event()
        self.release_close = asyncio.Event()

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled.set()
            await self.release_close.wait()


class _ProbeProvider:
    provider_name = "openai"
    model = "gpt-test"

    def __init__(self, stream: _DoneThenOpenStream) -> None:
        self._stream = stream
        self.calls = 0

    def chat(self, *args: Any, **kwargs: Any) -> _DoneThenOpenStream:
        del args, kwargs
        self.calls += 1
        return self._stream


@pytest.mark.asyncio
async def test_gateway_probe_closes_usage_accounted_physical_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _DoneThenOpenStream()
    provider = _ProbeProvider(stream)
    sink = _RecordingSink()
    close_timeouts: list[float | None] = []
    real_account_provider_stream = usage_accounting.account_provider_stream

    def recording_account_provider_stream(*args: Any, **kwargs: Any) -> Any:
        close_timeouts.append(kwargs.get("close_timeout"))
        return real_account_provider_stream(*args, **kwargs)

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: provider,
    )
    monkeypatch.setattr(
        usage_accounting,
        "account_provider_stream",
        recording_account_provider_stream,
    )

    result = await _usage_accounted_provider_probe(
        sink,
        provider_id="openai",
        model="gpt-test",
        api_key="synthetic-test-key",
        api_key_env="",
        base_url="https://provider.invalid/v1",
        proxy="",
        allow_default_api_key_env=False,
        mode="model",
    )

    assert result.ok is True
    assert provider.calls == 1
    assert close_timeouts == [None]
    assert stream.close_calls == 1
    assert len(sink.started) == 1
    assert len(sink.finalized) == 1
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_gateway_probe_counts_cancellation_resistant_physical_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _CancellationResistantCloseStream()
    provider = _ProbeProvider(stream)
    sink = _RecordingSink()
    monkeypatch.setattr(
        "opensquilla.onboarding.probe.build_provider",
        lambda *args, **kwargs: provider,
    )
    monkeypatch.setattr(probe_module, "_PROBE_STREAM_CLOSE_TIMEOUT_SECONDS", 0.01)

    assert probe_module.active_provider_probe_cleanup_tasks() == 0
    try:
        result = await _usage_accounted_provider_probe(
            sink,
            provider_id="openai",
            model="gpt-test",
            api_key="synthetic-test-key",
            api_key_env="",
            base_url="https://provider.invalid/v1",
            proxy="",
            allow_default_api_key_env=False,
            mode="model",
        )

        assert result.ok is True
        assert stream.close_started.is_set()
        assert stream.close_cancelled.is_set()
        assert stream.close_calls == 1
        assert probe_module.active_provider_probe_cleanup_tasks() == 1
    finally:
        stream.release_close.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if probe_module.active_provider_probe_cleanup_tasks() == 0:
                break

    assert probe_module.active_provider_probe_cleanup_tasks() == 0
    assert len(sink.finalized) == 1
    assert sink.unknown == []
