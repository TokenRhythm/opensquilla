from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.steps import squilla_router as squilla_router_step
from opensquilla.engine.types import ErrorEvent, RouterControlReplayEvent
from opensquilla.gateway.config import (
    GatewayConfig,
    SquillaRouterConfig,
    _router_tier_profile_defaults,
)
from opensquilla.provider import (
    DoneEvent as ProviderDone,
)
from opensquilla.provider import (
    Message,
    ModelInfo,
)
from opensquilla.provider import (
    TextDeltaEvent as ProviderText,
)
from opensquilla.provider import (
    ToolUseEndEvent as ProviderToolEnd,
)
from opensquilla.provider import (
    ToolUseStartEvent as ProviderToolStart,
)
from opensquilla.session.compaction import CompactionResult
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage
from opensquilla.tools import get_default_registry
from opensquilla.tools.types import CallerKind, ToolContext

_MODEL = "deepseek/deepseek-v4-pro"


class _CountingProvider:
    provider_name = "openrouter"

    def __init__(self, transcript_read_count: Callable[[], int]) -> None:
        self._transcript_read_count = transcript_read_count
        self._model = _MODEL
        self._api_key = "test-key"
        self._base_url = "https://example.invalid/v1"
        self.read_counts_at_dispatch: list[int] = []

    @property
    def model(self) -> str:
        return self._model

    def chat(
        self,
        messages: list[Message],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        self.read_counts_at_dispatch.append(self._transcript_read_count())
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="ok")
        yield ProviderDone(model=self.model)

    async def list_models(self) -> list[ModelInfo]:
        return []


class _ReplayProvider(_CountingProvider):
    def chat(
        self,
        messages: list[Message],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        self.read_counts_at_dispatch.append(self._transcript_read_count())
        return self._replay_stream(len(self.read_counts_at_dispatch))

    async def _replay_stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderText(text="discarded partial")
            yield ProviderToolStart(tool_use_id="tool-1", tool_name="router_control")
            yield ProviderToolEnd(
                tool_use_id="tool-1",
                tool_name="router_control",
                arguments={
                    "action": "set_hold",
                    "target_id": "tier:c3",
                    "evidence": "use c3",
                },
            )
            yield ProviderDone(model=self.model)
            return
        yield ProviderText(text="replayed final")
        yield ProviderDone(model=self.model)


class _RetryProvider(_CountingProvider):
    def chat(
        self,
        messages: list[Message],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        self.read_counts_at_dispatch.append(self._transcript_read_count())
        return self._retry_stream(len(self.read_counts_at_dispatch))

    async def _retry_stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderDone(
                model=self.model,
                stop_reason="stop",
                input_tokens=1,
                output_tokens=0,
            )
            return
        yield ProviderText(text="retry ok")
        yield ProviderDone(
            model=self.model,
            stop_reason="stop",
            input_tokens=1,
            output_tokens=1,
        )


class _SelectorClone:
    def __init__(self, provider: _CountingProvider) -> None:
        self.provider = provider
        self.current_config = SimpleNamespace(
            provider=provider.provider_name,
            model=provider.model,
        )

    def override_model(self, model: str) -> None:
        self.provider._model = model
        self.current_config = SimpleNamespace(
            provider=self.provider.provider_name,
            model=model,
        )

    def resolve(self) -> _CountingProvider:
        return self.provider


class _Selector:
    def __init__(self, provider: _CountingProvider) -> None:
        self.provider = provider

    def clone(self) -> _SelectorClone:
        return _SelectorClone(self.provider)


class _Strategy:
    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict[str, Any]] | None = None,
        **kwargs: object,
    ) -> tuple[str, float, str, dict[str, Any]]:
        return "c1", 0.9, "snapshot-runtime", {"route_class": "R1"}


def _config() -> GatewayConfig:
    return GatewayConfig(llm={"provider": "openrouter", "model": _MODEL})


def _tool_context() -> ToolContext:
    return ToolContext(is_owner=True, caller_kind=CallerKind.CLI)


async def _run(runner: TurnRunner, session_key: str) -> list[Any]:
    return [
        event
        async for event in runner.run(
            "current request",
            session_key,
            tool_context=_tool_context(),
            history_has_persisted_user=False,
            no_memory_capture=True,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_key",
    [
        "agent:main:snapshot-normal",
        "cron:snapshot-normal",
        "subagent:snapshot-normal",
    ],
)
async def test_normal_turn_reads_transcript_once_before_provider(
    monkeypatch: pytest.MonkeyPatch,
    session_key: str,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    await manager.create(session_key)
    original_get_transcript = manager.get_transcript
    read_count = 0

    async def _counted_get_transcript(
        key: str,
        limit: int | None = None,
    ) -> list[TranscriptEntry]:
        nonlocal read_count
        read_count += 1
        return await original_get_transcript(key, limit=limit)

    monkeypatch.setattr(manager, "get_transcript", _counted_get_transcript)
    provider = _CountingProvider(lambda: read_count)
    runner = TurnRunner(
        provider_selector=_Selector(provider),
        session_manager=manager,
        config=_config(),
    )

    try:
        events = await _run(runner, session_key)
    finally:
        await storage.close()

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert provider.read_counts_at_dispatch == [1]
    assert read_count == 1


@pytest.mark.asyncio
async def test_provider_retry_reuses_loaded_transcript_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:snapshot-provider-retry"
    await manager.create(session_key)
    original_get_transcript = manager.get_transcript
    read_count = 0

    async def _counted_get_transcript(
        key: str,
        limit: int | None = None,
    ) -> list[TranscriptEntry]:
        nonlocal read_count
        read_count += 1
        return await original_get_transcript(key, limit=limit)

    monkeypatch.setattr(manager, "get_transcript", _counted_get_transcript)
    provider = _RetryProvider(lambda: read_count)
    runner = TurnRunner(
        provider_selector=_Selector(provider),
        session_manager=manager,
        config=GatewayConfig(
            llm={"provider": "openrouter", "model": _MODEL},
            agent_max_provider_retries=1,
        ),
    )

    try:
        events = await _run(runner, session_key)
    finally:
        await storage.close()

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert provider.read_counts_at_dispatch == [1, 1]
    assert read_count == 1


@pytest.mark.asyncio
async def test_router_read_failure_is_retried_by_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "cron:snapshot-router-retry"
    await manager.create(session_key)
    await manager.append_message(session_key, "assistant", "prior answer")
    original_get_transcript = manager.get_transcript
    read_count = 0

    async def _fail_once_then_read(
        key: str,
        limit: int | None = None,
    ) -> list[TranscriptEntry]:
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            raise RuntimeError("router read failed")
        return await original_get_transcript(key, limit=limit)

    monkeypatch.setattr(manager, "get_transcript", _fail_once_then_read)
    provider = _CountingProvider(lambda: read_count)
    runner = TurnRunner(
        provider_selector=_Selector(provider),
        session_manager=manager,
        config=_config(),
    )

    try:
        events = await _run(runner, session_key)
    finally:
        await storage.close()

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert provider.read_counts_at_dispatch == [2]
    assert read_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("compaction_result", "expected_read_count"),
    [
        pytest.param(
            CompactionResult(
                summary="compacted durable history",
                kept_entries=[],
                removed_count=1,
                chunks_processed=1,
                summary_source="fallback",
                tokens_before=10_000,
                tokens_after=8,
                remaining_budget_tokens=56,
            ),
            2,
            id="durable-rows-changed",
        ),
        pytest.param(
            CompactionResult(
                summary="rolled checkpoint",
                kept_entries=[],
                removed_count=0,
                chunks_processed=1,
                summary_source="fallback",
                tokens_before=10_000,
                tokens_after=8,
                remaining_budget_tokens=56,
                replaced_previous_summary=True,
            ),
            1,
            id="checkpoint-only",
        ),
        pytest.param(
            CompactionResult(
                summary="",
                kept_entries=[],
                removed_count=0,
                chunks_processed=0,
                summary_source="skipped",
                tokens_before=10_000,
                tokens_after=10_000,
                remaining_budget_tokens=0,
                skip_reason="stale_preimage",
            ),
            1,
            id="stale-preimage",
        ),
    ],
)
async def test_preflight_invalidates_snapshot_only_when_transcript_rows_change(
    monkeypatch: pytest.MonkeyPatch,
    compaction_result: CompactionResult,
    expected_read_count: int,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:snapshot-compacted"
    node = await manager.create(session_key)
    compacted = False
    compaction_called = False
    read_count = 0
    before = TranscriptEntry(
        session_id=node.session_id,
        session_key=session_key,
        message_id="before-compaction",
        role="assistant",
        content="old durable history",
        token_count=10_000,
    )
    after = TranscriptEntry(
        session_id=node.session_id,
        session_key=session_key,
        message_id="after-compaction",
        role="system",
        content="[Context Summary] compacted durable history",
        token_count=8,
    )

    async def _versioned_get_transcript(
        key: str,
        limit: int | None = None,
    ) -> list[TranscriptEntry]:
        nonlocal read_count
        assert key == session_key
        assert limit is None
        read_count += 1
        return [after] if compacted else [before]

    async def _compact_with_result(
        key: str,
        context_window_tokens: int,
        config: Any = None,
        **kwargs: Any,
    ) -> CompactionResult:
        nonlocal compacted, compaction_called
        assert key == session_key
        assert context_window_tokens == 64
        compaction_called = True
        compacted = bool(
            compaction_result.summary and compaction_result.removed_count > 0
        )
        return compaction_result

    monkeypatch.setattr(manager, "get_transcript", _versioned_get_transcript)
    monkeypatch.setattr(manager, "compact_with_result", _compact_with_result)
    provider = _CountingProvider(lambda: read_count)
    runner = TurnRunner(
        provider_selector=_Selector(provider),
        session_manager=manager,
        config=_config(),
    )
    original_preflight = runner._maybe_preflight_compact

    async def _force_small_preflight_window(
        key: str,
        context_window_tokens: int,
        **kwargs: Any,
    ) -> None:
        kwargs["history_capacity_tokens"] = 64
        kwargs["history_capacity_chars"] = 256
        await original_preflight(key, 64, **kwargs)

    async def _checkpoint_succeeds(*args: Any, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(runner, "_maybe_preflight_compact", _force_small_preflight_window)
    monkeypatch.setattr(runner, "_record_checkpoint_before_compaction", _checkpoint_succeeds)
    monkeypatch.setattr(runner, "_pre_compaction_flush_enabled", lambda: False)

    try:
        events = await _run(runner, session_key)
    finally:
        await storage.close()

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert compaction_called is True
    assert provider.read_counts_at_dispatch == [expected_read_count]
    assert read_count == expected_read_count


@pytest.mark.asyncio
async def test_router_control_replay_uses_fresh_turn_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(squilla_router_step, "_get_strategy", lambda _cfg: _Strategy())
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:snapshot-router-control-replay"
    await manager.create(session_key)
    original_get_transcript = manager.get_transcript
    read_count = 0

    async def _counted_get_transcript(
        key: str,
        limit: int | None = None,
    ) -> list[TranscriptEntry]:
        nonlocal read_count
        read_count += 1
        return await original_get_transcript(key, limit=limit)

    monkeypatch.setattr(manager, "get_transcript", _counted_get_transcript)
    provider = _ReplayProvider(lambda: read_count)
    config = GatewayConfig(
        llm={"provider": "openrouter", "model": _MODEL},
        squilla_router=SquillaRouterConfig(
            enabled=True,
            rollout_phase="full",
            require_router_runtime=False,
            tiers=_router_tier_profile_defaults("openrouter"),
        ),
    )
    runner = TurnRunner(
        provider_selector=_Selector(provider),
        session_manager=manager,
        tool_registry=get_default_registry(),
        config=config,
    )

    try:
        events = await _run(runner, session_key)
    finally:
        await storage.close()

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert len([event for event in events if isinstance(event, RouterControlReplayEvent)]) == 1
    assert provider.read_counts_at_dispatch == [1, 2]
    assert read_count == 2
