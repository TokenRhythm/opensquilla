"""Tests for pre-flight compaction (Feature D).

Covers:
- Pre-flight triggers when transcript exceeds the configured context-window ratio
- Pre-flight does NOT trigger when under threshold
- cron: and subagent: sessions are skipped
- Missing sessions don't error
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from opensquilla.engine import runtime as runtime_module
from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import Message, ModelInfo
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.session import compaction as compaction_module
from opensquilla.session.compaction import CompactionConfig
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.types import CallerKind, ToolContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(content: str, role: str = "user") -> TranscriptEntry:
    return TranscriptEntry(
        session_id="test-session-id",
        session_key="test:key",
        role=role,
        content=content,
    )


def _flush_enabled_config(**overrides: Any) -> SimpleNamespace:
    memory = {
        "flush_enabled": True,
        "flush_pre_compaction": True,
        "flush_timeout_seconds": 0.25,
        "flush_background_timeout_seconds": 120.0,
        "flush_compaction_requires_safe_receipt": False,
    }
    memory.update(overrides)
    return SimpleNamespace(memory=SimpleNamespace(**memory))


def _make_assistant_tool_entry(content: str, tool_calls: list[dict[str, Any]]) -> TranscriptEntry:
    return TranscriptEntry(
        session_id="test-session-id",
        session_key="test:key",
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        token_count=1,
    )


def _make_assistant_reasoning_entry(content: str, reasoning_content: str) -> TranscriptEntry:
    return TranscriptEntry(
        session_id="test-session-id",
        session_key="test:key",
        role="assistant",
        content=content,
        reasoning_content=reasoning_content,
        token_count=1,
    )


def test_routed_small_window_cannot_lower_durable_history_boundary() -> None:
    assert runtime_module._durable_compaction_window_tokens(
        8_000,
        stable_consumer_window_tokens=128_000,
        routing_applied=True,
    ) == 128_000
    assert runtime_module._durable_compaction_window_tokens(
        8_000,
        stable_consumer_window_tokens=128_000,
        routing_applied=False,
    ) == 8_000
    # A compaction deployment is deliberately not an input to this decision:
    # its larger window may size the summary request, never durable pressure.
    assert runtime_module._durable_compaction_window_tokens(
        8_000,
        stable_consumer_window_tokens=8_000,
        routing_applied=True,
    ) == 8_000


def test_durable_history_uses_pre_router_deployment_identity() -> None:
    metadata = {
        "routing_applied": True,
        "durable_base_provider": "openai",
        "durable_base_model": "large-session-model",
        # These mutable fields now describe this turn's small routed leg.
        "executed_provider": "openrouter",
        "executed_model": "small-route-model",
    }

    assert runtime_module._stable_consumer_execution_identity(metadata) == (
        "openai",
        "large-session-model",
    )


def _checkpoint_receipt() -> SimpleNamespace:
    return SimpleNamespace(
        scope="checkpoint",
        status="checkpoint_saved",
        source_path="memory/.checkpoints/s/turn.jsonl",
        content_hash="h1",
    )


def _flush_receipt(**overrides):
    payload = {
        "mode": "llm",
        "error": None,
        "indexed_chunk_count": 1,
        "integrity_status": "ok",
        "output_coverage_status": "ok",
        "invalid_candidate_count": 0,
        "candidate_missing_ids": [],
        "obligation_status": "ok",
        "obligation_missing_ids": [],
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


class _FakeCompactionProvider:
    provider_name = "openai"

    def __init__(self, model: str = "provider/model") -> None:
        self._api_key = "preflight-provider-key"
        self._model = model
        self._base_url = "https://openrouter.ai/api/v1"

    @property
    def model(self) -> str:
        return self._model

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator:
        return self._stream()

    async def _stream(self) -> AsyncIterator:
        yield ProviderText(text="ok")
        yield ProviderDone(stop_reason="end_turn", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[ModelInfo]:
        return []


class _FakeSelectorClone:
    current_config = SimpleNamespace(model="provider/model")

    def __init__(self, provider: _FakeCompactionProvider) -> None:
        self.provider = provider
        self.override_calls: list[str] = []

    def override_model(self, model: str) -> None:
        self.override_calls.append(model)
        self.provider._model = model
        self.current_config = SimpleNamespace(model=model)

    def resolve(self) -> _FakeCompactionProvider:
        return self.provider


class _FakeProviderSelector:
    current_config = SimpleNamespace(model="provider/model")

    def __init__(self, provider: _FakeCompactionProvider | None = None) -> None:
        self.provider = provider or _FakeCompactionProvider()
        self.clone_instance = _FakeSelectorClone(self.provider)
        self.override_calls: list[str] = []

    def clone(self) -> _FakeSelectorClone:
        return self.clone_instance

    def override_model(self, model: str) -> None:
        self.override_calls.append(model)

    def resolve(self) -> _FakeCompactionProvider:
        return self.provider


class _ResultCompactionSessionManager:
    def __init__(self, transcript: list[TranscriptEntry]) -> None:
        self._transcript = transcript
        self.compact_with_result_calls: list[tuple[str, int, object | None]] = []
        self.compact_with_result_kwargs: list[dict[str, object | None]] = []

    async def get_transcript(self, session_key: str) -> list[TranscriptEntry]:
        return list(self._transcript)

    async def compact_with_result(
        self,
        session_key: str,
        context_window_tokens: int,
        config: object | None = None,
        **kwargs,
    ) -> SimpleNamespace:
        self.compact_with_result_calls.append((session_key, context_window_tokens, config))
        self.compact_with_result_kwargs.append(dict(kwargs))
        return SimpleNamespace(
            summary="summary text",
            kept_entries=[{"role": "assistant", "content": "tail"}],
            removed_count=4,
            chunks_processed=3,
            summary_source="llm",
            tokens_before=1000,
            tokens_after=200,
            remaining_budget_tokens=800,
        )


class _FailingResultCompactionSessionManager(_ResultCompactionSessionManager):
    async def compact_with_result(
        self,
        session_key: str,
        context_window_tokens: int,
        config: object | None = None,
        **kwargs,
    ) -> SimpleNamespace:
        self.compact_with_result_calls.append((session_key, context_window_tokens, config))
        self.compact_with_result_kwargs.append(dict(kwargs))
        raise RuntimeError("preimage write failed")


class _StaleResultCompactionSessionManager(_ResultCompactionSessionManager):
    async def compact_with_result(
        self,
        session_key: str,
        context_window_tokens: int,
        config: object | None = None,
        **kwargs,
    ) -> SimpleNamespace:
        self.compact_with_result_calls.append((session_key, context_window_tokens, config))
        self.compact_with_result_kwargs.append(dict(kwargs))
        return SimpleNamespace(
            summary="",
            kept_entries=list(self._transcript),
            removed_count=0,
            chunks_processed=0,
            summary_source="skipped",
            skip_reason="stale_preimage",
            tokens_before=2400,
            tokens_after=2400,
            remaining_budget_tokens=0,
        )


def _assert_armed_compaction_call(
    calls: list[tuple[str, int, object | None]],
    session_key: str,
    context_window: int,
) -> CompactionConfig:
    assert len(calls) == 1
    called_key, called_window, config = calls[0]
    assert (called_key, called_window) == (session_key, context_window)
    assert isinstance(config, CompactionConfig)
    assert config.operation_id
    assert config.deadline_at_monotonic is not None
    return config


@pytest.fixture
async def session_mgr(tmp_path):
    storage = SessionStorage(":memory:")
    await storage.connect()
    mgr = SessionManager(storage, checkpoint_workspace_dir=tmp_path)
    yield mgr
    await storage.close()


# ---------------------------------------------------------------------------
# Tests: _maybe_preflight_compact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_no_session_manager_is_noop() -> None:
    """When session_manager is None, pre-flight silently returns."""
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=None)
    # Should not raise
    await runner._maybe_preflight_compact("some:session", 200_000)


@pytest.mark.asyncio
async def test_preflight_skips_cron_sessions() -> None:
    """cron: prefixed sessions are skipped regardless of token count."""
    mock_sm = MagicMock()
    mock_sm.compact = AsyncMock()
    mock_sm.get_transcript = AsyncMock()

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=mock_sm)
    await runner._maybe_preflight_compact("cron:daily-job", 200_000)

    mock_sm.get_transcript.assert_not_called()
    mock_sm.compact.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_skips_subagent_sessions() -> None:
    """subagent: prefixed sessions are skipped regardless of token count."""
    mock_sm = MagicMock()
    mock_sm.compact = AsyncMock()
    mock_sm.get_transcript = AsyncMock()

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=mock_sm)
    await runner._maybe_preflight_compact("subagent:worker-1", 200_000)

    mock_sm.get_transcript.assert_not_called()
    mock_sm.compact.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_missing_session_does_not_error() -> None:
    """KeyError from get_transcript (session doesn't exist) is swallowed."""
    mock_sm = MagicMock()
    mock_sm.compact = AsyncMock()
    mock_sm.get_transcript = AsyncMock(side_effect=KeyError("Session not found"))

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=mock_sm)
    # Should not raise
    await runner._maybe_preflight_compact("missing:session", 200_000)

    mock_sm.compact.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_empty_transcript_is_noop() -> None:
    """Empty transcript skips compaction."""
    mock_sm = MagicMock()
    mock_sm.compact = AsyncMock()
    mock_sm.get_transcript = AsyncMock(return_value=[])

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=mock_sm)
    await runner._maybe_preflight_compact("user:session", 200_000)

    mock_sm.compact.assert_not_called()


def test_current_turn_suffix_uses_bound_prompt_and_includes_queued_prompts() -> None:
    older_user = _make_entry("older user")
    older_assistant = _make_entry("older assistant", role="assistant")
    active_user = _make_entry("active user")
    queued_user = _make_entry("queued user")

    protected = TurnRunner._protected_current_turn_suffix_count(
        [older_user, older_assistant, active_user, queued_user],
        history_has_persisted_user=True,
        bound_user_message_id=active_user.message_id,
    )

    assert protected == 2


def test_current_turn_suffix_fails_closed_when_bound_prompt_is_missing() -> None:
    transcript = [
        _make_entry("older user"),
        _make_entry("older assistant", role="assistant"),
    ]

    protected = TurnRunner._protected_current_turn_suffix_count(
        transcript,
        history_has_persisted_user=True,
        bound_user_message_id="missing-active-user",
    )

    assert protected == len(transcript)


@pytest.mark.asyncio
async def test_preflight_protects_active_and_queued_prompts_from_durable_compaction() -> None:
    context_window = 1000
    active_user = _make_entry("active user")
    active_user.token_count = 10
    queued_user = _make_entry("queued user")
    queued_user.token_count = 10
    old_user = _make_entry("old user")
    old_user.token_count = 1_000
    old_assistant = _make_entry("old assistant", role="assistant")
    old_assistant.token_count = 1_000
    transcript = [old_user, old_assistant, active_user, queued_user]
    sm = _ResultCompactionSessionManager(transcript)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=sm)

    await runner._maybe_preflight_compact(
        "user:session",
        context_window,
        history_has_persisted_user=True,
        bound_user_message_id=active_user.message_id,
    )

    config = _assert_armed_compaction_call(
        sm.compact_with_result_calls,
        "user:session",
        context_window,
    )
    assert config.protected_recent_messages == 2


@pytest.mark.asyncio
async def test_preflight_compacts_to_final_envelope_history_capacity() -> None:
    context_window = 1000
    history_capacity = 600
    transcript = [
        TranscriptEntry(
            session_id="test-session-id",
            session_key="user:session",
            role="user",
            content="old history",
            token_count=900,
        ),
        TranscriptEntry(
            session_id="test-session-id",
            session_key="user:session",
            role="assistant",
            content="recent answer",
            token_count=20,
        ),
    ]
    sm = _ResultCompactionSessionManager(transcript)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=sm)

    await runner._maybe_preflight_compact(
        "user:session",
        context_window,
        history_capacity_tokens=history_capacity,
    )

    _assert_armed_compaction_call(
        sm.compact_with_result_calls,
        "user:session",
        history_capacity,
    )


@pytest.mark.asyncio
async def test_preflight_character_pressure_uses_final_envelope_capacity() -> None:
    transcript = [
        TranscriptEntry(
            session_id="test-session-id",
            session_key="user:session",
            role="user",
            content="character-heavy history " + ("x" * 4_000),
            token_count=1,
        ),
        TranscriptEntry(
            session_id="test-session-id",
            session_key="user:session",
            role="assistant",
            content="short answer",
            token_count=1,
        ),
    ]
    sm = _ResultCompactionSessionManager(transcript)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=sm)

    await runner._maybe_preflight_compact(
        "user:session",
        20_000,
        history_capacity_tokens=20_000,
        history_capacity_chars=1_000,
    )

    _assert_armed_compaction_call(
        sm.compact_with_result_calls,
        "user:session",
        20_000,
    )
    assert len(sm.compact_with_result_kwargs) == 1
    assert sm.compact_with_result_kwargs[0]["context_window_chars"] == 1_000


@pytest.mark.asyncio
async def test_preflight_skips_when_fixed_envelope_exhausts_input_budget() -> None:
    sm = _ResultCompactionSessionManager(
        [
            TranscriptEntry(
                session_id="test-session-id",
                session_key="user:session",
                role="user",
                content="old history",
                token_count=900,
            )
        ]
    )
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=sm)

    await runner._maybe_preflight_compact(
        "user:session",
        1000,
        history_capacity_tokens=0,
    )

    assert sm.compact_with_result_calls == []


@pytest.mark.asyncio
async def test_preflight_skips_durable_work_when_active_prompt_alone_is_too_large() -> None:
    context_window = 1000
    active_user = TranscriptEntry(
        session_id="test-session-id",
        session_key="user:session",
        role="user",
        content="active user",
        token_count=1000,
    )
    transcript = [
        TranscriptEntry(
            session_id="test-session-id",
            session_key="user:session",
            role="assistant",
            content="small old history",
            token_count=10,
        ),
        active_user,
    ]
    sm = _ResultCompactionSessionManager(transcript)
    sm.record_memory_checkpoint = AsyncMock()
    flush_service = MagicMock()
    flush_service.execute = AsyncMock(return_value=_flush_receipt())
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=sm,
        session_flush_service=flush_service,
        config=_flush_enabled_config(),
    )

    await runner._maybe_preflight_compact(
        "user:session",
        context_window,
        history_has_persisted_user=True,
        bound_user_message_id=active_user.message_id,
    )

    assert sm.compact_with_result_calls == []
    sm.record_memory_checkpoint.assert_not_called()
    flush_service.execute.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_does_not_attribute_active_prompt_pressure_to_history() -> None:
    context_window = 1000
    old_history = TranscriptEntry(
        session_id="test-session-id",
        session_key="user:session",
        role="assistant",
        content="old history",
        token_count=400,
    )
    active_user = TranscriptEntry(
        session_id="test-session-id",
        session_key="user:session",
        role="user",
        content="active user",
        token_count=500,
    )
    sm = _ResultCompactionSessionManager([old_history, active_user])
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=sm)

    await runner._maybe_preflight_compact(
        "user:session",
        context_window,
        history_has_persisted_user=True,
        bound_user_message_id=active_user.message_id,
    )

    assert sm.compact_with_result_calls == []


@pytest.mark.asyncio
async def test_preflight_skips_when_only_active_request_is_present() -> None:
    active_user = _make_entry("active user")
    sm = _ResultCompactionSessionManager([active_user])
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=sm)

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact(
            "user:session",
            1000,
            history_has_persisted_user=True,
            bound_user_message_id=active_user.message_id,
        )

    assert sm.compact_with_result_calls == []


@pytest.mark.asyncio
async def test_preflight_legacy_compactor_uses_id_safe_ephemeral_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "user:legacy-protected"
    active_user = TranscriptEntry(
        session_id="test-session-id",
        session_key=session_key,
        role="user",
        content="active user",
        token_count=10,
    )
    queued_user = TranscriptEntry(
        session_id="test-session-id",
        session_key=session_key,
        role="user",
        content="queued user",
        token_count=10,
    )
    transcript = [
        TranscriptEntry(
            session_id="test-session-id",
            session_key=session_key,
            role="user",
            content="old user",
            token_count=600,
        ),
        TranscriptEntry(
            session_id="test-session-id",
            session_key=session_key,
            role="assistant",
            content="old assistant",
            token_count=600,
        ),
        active_user,
        queued_user,
    ]
    durable_calls: list[tuple[str, int]] = []
    manager = MagicMock()
    manager.get_transcript = AsyncMock(return_value=transcript)

    async def legacy_compact(key: str, window: int) -> str:
        durable_calls.append((key, window))
        return "unsafe durable summary"

    manager.compact = AsyncMock(side_effect=legacy_compact)
    emergency_requests: list[Any] = []

    async def emergency_compact(request: Any) -> SimpleNamespace:
        emergency_requests.append(request)
        return SimpleNamespace(
            summary="safe request-only summary",
            kept_entries=request.entries[2:],
            removed_count=2,
            chunks_processed=1,
            tokens_before=1220,
            tokens_after=20,
        )

    monkeypatch.setattr(
        "opensquilla.session.compaction.compact_context",
        emergency_compact,
    )
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)

    await runner._maybe_preflight_compact(
        session_key,
        1000,
        history_has_persisted_user=True,
        bound_user_message_id=active_user.message_id,
    )

    assert durable_calls == []
    assert len(emergency_requests) == 1
    assert emergency_requests[0].config.protected_recent_messages == 2

    class _HistoryCapture:
        provider = SimpleNamespace(provider_name="test")

        def __init__(self) -> None:
            self.history: list[Any] = []

        def set_history(self, history: list[Any]) -> None:
            self.history = history

    agent = _HistoryCapture()
    summary_context = await runner._load_history(
        agent,
        session_key,
        trim_last_user=True,
        bound_user_message_id=active_user.message_id,
    )

    assert agent.history == []
    assert summary_context is not None
    assert "safe request-only summary" in summary_context


@pytest.mark.asyncio
async def test_preflight_under_threshold_does_not_compact() -> None:
    """Transcript under the configured context-window ratio → no compaction."""
    # 100 tokens worth of content (estimate_tokens("x" * 400) ≈ 100 with len//4 fallback)
    entries = [_make_entry("x" * 400)]  # ~100 tokens

    mock_sm = MagicMock()
    mock_sm.compact = AsyncMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=mock_sm)
    # Threshold = 200_000 * 0.85 = 170_000 — 100 tokens is well under
    await runner._maybe_preflight_compact("user:session", 200_000)

    mock_sm.compact.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_uses_configured_compaction_ratio() -> None:
    """Operators can tune the preflight threshold without code changes."""
    context_window = 1000
    entries = [_make_entry("a")]

    mock_sm = MagicMock()
    mock_sm.compact = AsyncMock(return_value="summary text")
    mock_sm.get_transcript = AsyncMock(return_value=entries)

    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=mock_sm,
        config=SimpleNamespace(preflight_compact_ratio=0.5),
    )

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=600):
        await runner._maybe_preflight_compact("user:session", context_window)

    mock_sm.compact.assert_called_once_with("user:session", context_window)


@pytest.mark.asyncio
async def test_preflight_above_threshold_triggers_compact() -> None:
    """Transcript exceeding the default 85% context-window ratio triggers compaction."""
    # Use patch to control estimate_tokens so threshold math is deterministic
    context_window = 1000

    # Create entries whose total estimated tokens exceed threshold
    entries = [_make_entry("a" * 4000)]  # len//4 = 1000 tokens > 850

    mock_sm = MagicMock()
    mock_sm.compact = AsyncMock(return_value="summary text")
    mock_sm.get_transcript = AsyncMock(return_value=entries)

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=mock_sm)

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact("user:session", context_window)

    mock_sm.compact.assert_called_once_with("user:session", context_window)


@pytest.mark.asyncio
async def test_preflight_checkpoint_runs_before_compact() -> None:
    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    calls: list[str] = []
    mock_sm = MagicMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)
    mock_sm.compact = AsyncMock(
        side_effect=lambda *args, **kwargs: calls.append("compact") or "summary"
    )
    mock_sm.record_memory_checkpoint = AsyncMock(
        side_effect=lambda *args, **kwargs: calls.append("checkpoint")
        or _checkpoint_receipt()
    )

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=mock_sm)

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact("agent:ops:long-session", context_window)

    assert calls[0] == "checkpoint"
    assert "compact" in calls


@pytest.mark.asyncio
async def test_preflight_compacts_when_distill_fails_after_checkpoint() -> None:
    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    calls: list[str] = []
    mock_sm = MagicMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)
    mock_sm.record_memory_checkpoint = AsyncMock(
        side_effect=lambda *args, **kwargs: calls.append("checkpoint")
        or _checkpoint_receipt()
    )
    mock_sm.compact = AsyncMock(
        side_effect=lambda *args, **kwargs: calls.append("compact") or "summary text"
    )

    async def _flush_fails(*args: Any, **kwargs: Any) -> SimpleNamespace:
        calls.append("flush")
        raise RuntimeError("bad json")

    flush_service = MagicMock()
    flush_service.execute = AsyncMock(side_effect=_flush_fails)
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=mock_sm,
        session_flush_service=flush_service,
        config=SimpleNamespace(
            memory=SimpleNamespace(
                flush_enabled=True,
                flush_pre_compaction=True,
                flush_timeout_seconds=0.25,
                flush_background_timeout_seconds=42.0,
            )
        ),
    )

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact("agent:ops:long-session", context_window)

    assert calls[:2] == ["checkpoint", "compact"]
    await asyncio.sleep(0)
    assert "flush" in calls
    mock_sm.compact.assert_awaited_once_with("agent:ops:long-session", context_window)


@pytest.mark.asyncio
async def test_preflight_checkpoint_failure_prevents_destructive_compaction() -> None:
    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    mock_sm = MagicMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)
    mock_sm.record_memory_checkpoint = AsyncMock(
        side_effect=RuntimeError("checkpoint write failed")
    )
    mock_sm.compact = AsyncMock(return_value="summary text")
    flush_service = MagicMock()
    flush_service.execute = AsyncMock(return_value=_flush_receipt())
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=mock_sm,
        session_flush_service=flush_service,
        config=SimpleNamespace(
            memory=SimpleNamespace(
                flush_enabled=True,
                flush_pre_compaction=True,
                flush_timeout_seconds=0.25,
                flush_background_timeout_seconds=42.0,
            )
        ),
    )

    with (
        patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000),
        pytest.raises(RuntimeError, match="checkpoint write failed"),
    ):
        await runner._maybe_preflight_compact("agent:ops:long-session", context_window)

    flush_service.execute.assert_not_called()
    mock_sm.compact.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_completed_event_reports_compaction_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_window = 1000
    entries = [_make_entry("a" * 4000)]
    sm = _ResultCompactionSessionManager(entries)
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        runtime_module,
        "notify_compaction",
        lambda session_key, **payload: events.append((session_key, payload)),
    )
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=sm)

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact("user:session", context_window)

    compaction_config = _assert_armed_compaction_call(
        sm.compact_with_result_calls,
        "user:session",
        context_window,
    )
    assert [(key, payload["status"]) for key, payload in events] == [
        ("user:session", "started"),
        ("user:session", "observed"),
        ("user:session", "observed"),
        ("user:session", "completed"),
    ]
    compaction_ids = {payload.get("compaction_id") for _, payload in events}
    assert len(compaction_ids) == 1
    assert None not in compaction_ids
    assert compaction_config.operation_id in compaction_ids
    assert events[0][1]["event"] == "compaction.triggered"
    assert events[1][1]["event"] == "compaction.chunk_summarized"
    assert events[2][1]["event"] == "compaction.summary_verified"
    completed = events[-1][1]
    assert completed["applied"] is True
    assert completed["durability"] == "durable"
    assert completed["user_visible"] is True
    assert completed["event"] == "compaction.persisted"
    assert completed["event_chain"] == [
        "compaction.triggered",
        "compaction.chunk_summarized",
        "compaction.summary_verified",
        "compaction.persisted",
    ]
    assert completed["coverage_status"] == "unknown"
    assert completed["chunk_count"] == 3
    assert completed["summary_source"] == "llm"
    assert completed["removed_count"] == 4
    assert completed["kept_count"] == 1
    assert completed["tokens_after"] == 200
    assert completed["remaining_budget_tokens"] == 800


@pytest.mark.asyncio
async def test_preflight_counts_tool_call_arguments_when_deciding_to_compact() -> None:
    context_window = 1000
    entries = [
        _make_assistant_tool_entry(
            "small visible answer",
            [
                {
                    "type": "tool_use",
                    "tool_use_id": "write-stale",
                    "name": "write_file",
                    "input": {
                        "path": "index.html",
                        "content": "x" * 5000,
                    },
                }
            ],
        )
    ]
    mock_sm = MagicMock()
    mock_sm.compact = AsyncMock(return_value="summary text")
    mock_sm.get_transcript = AsyncMock(return_value=entries)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=mock_sm)

    with patch(
        "opensquilla.session.tokenizer.estimate_tokens",
        side_effect=lambda text: max(1, len(str(text)) // 4),
    ):
        await runner._maybe_preflight_compact("user:session", context_window)

    mock_sm.compact.assert_called_once_with("user:session", context_window)


@pytest.mark.asyncio
async def test_preflight_counts_reasoning_content_when_deciding_to_compact() -> None:
    context_window = 1000
    entries = [
        _make_assistant_reasoning_entry(
            "small visible answer",
            "reasoning " + ("r" * 5000),
        )
    ]
    mock_sm = MagicMock()
    mock_sm.compact = AsyncMock(return_value="summary text")
    mock_sm.get_transcript = AsyncMock(return_value=entries)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=mock_sm)

    with patch(
        "opensquilla.session.tokenizer.estimate_tokens",
        side_effect=lambda text: max(1, len(str(text)) // 4),
    ):
        await runner._maybe_preflight_compact("user:session", context_window)

    mock_sm.compact.assert_called_once_with("user:session", context_window)


@pytest.mark.asyncio
async def test_preflight_starts_full_transcript_flush_without_blocking_compact() -> None:
    """Preflight starts full-coverage memory flush in the background."""

    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    calls: list[str] = []

    mock_sm = MagicMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)

    async def compact(session_key, context_window_tokens):
        calls.append("compact")
        return "summary text"

    mock_sm.compact = AsyncMock(side_effect=compact)

    flush_service = MagicMock()

    async def flush_execute(*args, **kwargs):
        calls.append("flush")
        return _flush_receipt()

    flush_service.execute = AsyncMock(side_effect=flush_execute)

    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=mock_sm,
        session_flush_service=flush_service,
        config=_flush_enabled_config(),
    )

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact("agent:ops:long-session", context_window)

    assert calls == ["compact"]
    await asyncio.sleep(0)
    assert "flush" in calls
    flush_service.execute.assert_awaited_once_with(
        entries,
        "agent:ops:long-session",
        agent_id="ops",
        message_window=0,
        segment_mode="auto",
        timeout=120.0,
        raw_capture_policy="required",
        turn_id=ANY,
        checkpoint_exists=False,
    )
    mock_sm.compact.assert_awaited_once_with("agent:ops:long-session", context_window)


@pytest.mark.parametrize(
    "receipt",
    [
        _flush_receipt(mode="raw", raw_reason="no_provider"),
        _flush_receipt(integrity_status="missing_chunks"),
        _flush_receipt(output_coverage_status="coverage_warning"),
        _flush_receipt(invalid_candidate_count=1),
        _flush_receipt(candidate_missing_ids=["candidate-1"]),
        _flush_receipt(obligation_missing_ids=["obligation-1"]),
    ],
)
@pytest.mark.asyncio
async def test_preflight_degraded_flush_receipts_do_not_block_compaction(
    receipt: SimpleNamespace,
) -> None:
    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    mock_sm = MagicMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)
    mock_sm.compact = AsyncMock(return_value="summary text")

    flush_service = MagicMock()
    flush_service.execute = AsyncMock(return_value=receipt)
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=mock_sm,
        session_flush_service=flush_service,
        config=_flush_enabled_config(),
    )

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact("agent:ops:long-session", context_window)

    await asyncio.sleep(0)
    flush_service.execute.assert_awaited_once()
    mock_sm.compact.assert_awaited_once_with("agent:ops:long-session", context_window)


@pytest.mark.asyncio
async def test_preflight_strict_flush_receipt_skips_destructive_compaction() -> None:
    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    mock_sm = MagicMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)
    mock_sm.compact = AsyncMock(return_value="summary text")

    flush_service = MagicMock()
    flush_service.execute = AsyncMock(
        return_value=_flush_receipt(integrity_status="missing_chunks")
    )
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=mock_sm,
        session_flush_service=flush_service,
        config=SimpleNamespace(
            memory=SimpleNamespace(
                flush_enabled=True,
                flush_pre_compaction=True,
                flush_timeout_seconds=0.25,
                flush_background_timeout_seconds=42.0,
                flush_compaction_requires_safe_receipt=True,
            )
        ),
    )

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact("agent:ops:long-session", context_window)

    flush_service.execute.assert_awaited_once()
    mock_sm.compact.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_protect_flush_receipt_marks_degraded_forensic() -> None:
    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    sm = _ResultCompactionSessionManager(entries)
    flush_service = MagicMock()
    flush_service.execute = AsyncMock(
        return_value=_flush_receipt(integrity_status="missing_chunks")
    )
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=sm,
        session_flush_service=flush_service,
        config=SimpleNamespace(
            memory=SimpleNamespace(
                flush_enabled=True,
                flush_pre_compaction=True,
                flush_timeout_seconds=0.25,
                flush_background_timeout_seconds=42.0,
                flush_compaction_safety_mode="protect",
            )
        ),
    )

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact("agent:ops:long-session", context_window)

    await asyncio.sleep(0)
    flush_service.execute.assert_awaited_once()
    _assert_armed_compaction_call(
        sm.compact_with_result_calls,
        "agent:ops:long-session",
        context_window,
    )
    assert sm.compact_with_result_kwargs[0]["flush_receipt_status"] == "degraded_forensic"


@pytest.mark.asyncio
async def test_preflight_compact_failure_uses_emergency_ephemeral_history_trim() -> None:
    session_key = "agent:ops:preflight-emergency"
    context_window = 1000
    entries = [
        TranscriptEntry(
            session_id="test-session-id",
            session_key=session_key,
            role="user" if index % 2 == 0 else "assistant",
            content=f"historic message {index} " + ("x" * 500),
            token_count=300,
        )
        for index in range(8)
    ]
    sm = _FailingResultCompactionSessionManager(entries)
    flush_service = MagicMock()
    flush_service.execute = AsyncMock(return_value=_flush_receipt(mode="raw"))
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=sm,
        session_flush_service=flush_service,
        config=SimpleNamespace(
            memory=SimpleNamespace(
                flush_enabled=True,
                flush_pre_compaction=True,
                flush_timeout_seconds=0.25,
                flush_background_timeout_seconds=42.0,
                flush_compaction_safety_mode="protect",
            )
        ),
    )

    await runner._maybe_preflight_compact(session_key, context_window)

    class _HistoryCapture:
        provider = SimpleNamespace(provider_name="test")

        def __init__(self) -> None:
            self.history: list[Any] = []

        def set_history(self, history: list[Any]) -> None:
            self.history = history

    agent = _HistoryCapture()
    summary_context = await runner._load_history(agent, session_key, trim_last_user=False)

    _assert_armed_compaction_call(sm.compact_with_result_calls, session_key, context_window)
    assert len(await sm.get_transcript(session_key)) == len(entries)
    assert len(agent.history) < len(entries)
    assert summary_context is not None
    assert "emergency request-scoped compaction" in summary_context.lower()


@pytest.mark.asyncio
async def test_preflight_compact_failure_reports_emergency_without_failed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "agent:ops:preflight-emergency-event"
    context_window = 1000
    entries = [
        TranscriptEntry(
            session_id="test-session-id",
            session_key=session_key,
            role="user" if index % 2 == 0 else "assistant",
            content=f"historic message {index} " + ("x" * 500),
            token_count=300,
        )
        for index in range(8)
    ]
    sm = _FailingResultCompactionSessionManager(entries)
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        runtime_module,
        "notify_compaction",
        lambda session_key, **payload: events.append((session_key, payload)),
    )
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=sm)
    runner._compaction_failures[session_key] = runtime_module._CompactionFailureState(count=1)

    await runner._maybe_preflight_compact(session_key, context_window)

    statuses = [payload["status"] for _, payload in events]
    assert statuses == ["started", "emergency_ephemeral"]
    emergency = events[-1][1]
    assert emergency["applied"] is True
    assert emergency["durability"] == "request_scoped"
    assert emergency["user_visible"] is True
    assert emergency["reason"] == "compact_failed"
    assert emergency["flush_receipt_status"] == "emergency_ephemeral"
    assert runner._compaction_failures[session_key].count == 2


@pytest.mark.asyncio
async def test_preflight_open_circuit_still_uses_request_scoped_emergency_trim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "agent:ops:preflight-open-circuit"
    context_window = 10_000
    history_capacity = 1_500
    history_capacity_chars = 5_000
    entries = [
        TranscriptEntry(
            session_id="test-session-id",
            session_key=session_key,
            role="user" if index % 2 == 0 else "assistant",
            content=f"historic message {index} " + ("x" * 500),
            token_count=300,
        )
        for index in range(8)
    ]
    mock_sm = MagicMock()
    mock_sm.compact = AsyncMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        runtime_module,
        "notify_compaction",
        lambda session_key, **payload: events.append((session_key, payload)),
    )
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=mock_sm)
    runner._compaction_failures[session_key] = runtime_module._CompactionFailureState(
        count=3,
        opened_at=runtime_module.time.monotonic(),
    )
    emergency_requests: list[Any] = []
    original_compact_context = compaction_module.compact_context

    async def capture_emergency_request(request: Any) -> Any:
        emergency_requests.append(request)
        return await original_compact_context(request)

    monkeypatch.setattr(compaction_module, "compact_context", capture_emergency_request)

    await runner._maybe_preflight_compact(
        session_key,
        context_window,
        history_capacity_tokens=history_capacity,
        history_capacity_chars=history_capacity_chars,
    )

    mock_sm.compact.assert_not_awaited()
    assert [payload["status"] for _, payload in events] == ["emergency_ephemeral"]
    emergency = events[-1][1]
    assert emergency["reason"] == "durable_compaction_circuit_open"
    assert emergency["applied"] is True
    assert emergency["durability"] == "request_scoped"
    assert runner._compaction_failures[session_key].count == 3
    assert len(emergency_requests) == 1
    assert emergency_requests[0].context_window_tokens == history_capacity
    assert emergency_requests[0].context_window_chars == history_capacity_chars


@pytest.mark.asyncio
async def test_preflight_empty_summary_uses_emergency_ephemeral_history_trim() -> None:
    session_key = "agent:ops:preflight-empty-summary"
    context_window = 1000
    entries = [
        TranscriptEntry(
            session_id="test-session-id",
            session_key=session_key,
            role="user" if index % 2 == 0 else "assistant",
            content=f"historic message {index} " + ("x" * 500),
            token_count=300,
        )
        for index in range(8)
    ]

    class _EmptySummarySessionManager(_ResultCompactionSessionManager):
        async def compact_with_result(
            self,
            session_key: str,
            context_window_tokens: int,
            config: object | None = None,
            **kwargs,
        ) -> SimpleNamespace:
            self.compact_with_result_calls.append((session_key, context_window_tokens, config))
            self.compact_with_result_kwargs.append(dict(kwargs))
            return SimpleNamespace(
                summary="",
                kept_entries=entries,
                removed_count=0,
                chunks_processed=0,
                summary_source="skipped",
                tokens_before=2400,
                tokens_after=2400,
                remaining_budget_tokens=0,
            )

    sm = _EmptySummarySessionManager(entries)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=sm)

    await runner._maybe_preflight_compact(session_key, context_window)

    class _HistoryCapture:
        provider = SimpleNamespace(provider_name="test")

        def __init__(self) -> None:
            self.history: list[Any] = []

        def set_history(self, history: list[Any]) -> None:
            self.history = history

    agent = _HistoryCapture()
    summary_context = await runner._load_history(agent, session_key, trim_last_user=False)

    _assert_armed_compaction_call(sm.compact_with_result_calls, session_key, context_window)
    assert len(await sm.get_transcript(session_key)) == len(entries)
    assert len(agent.history) < len(entries)
    assert summary_context is not None
    assert "emergency request-scoped compaction" in summary_context.lower()


@pytest.mark.asyncio
async def test_preflight_stale_preimage_skip_does_not_use_emergency_trim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "agent:ops:preflight-stale-summary"
    context_window = 1000
    entries = [
        TranscriptEntry(
            session_id="test-session-id",
            session_key=session_key,
            role="user" if index % 2 == 0 else "assistant",
            content=f"historic message {index} " + ("x" * 500),
            token_count=300,
        )
        for index in range(8)
    ]
    sm = _StaleResultCompactionSessionManager(entries)
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        runtime_module,
        "notify_compaction",
        lambda session_key, **payload: events.append((session_key, payload)),
    )
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=sm)
    runner._compaction_failures[session_key] = runtime_module._CompactionFailureState(count=1)

    await runner._maybe_preflight_compact(session_key, context_window)

    _assert_armed_compaction_call(sm.compact_with_result_calls, session_key, context_window)
    assert runner.has_compacted_this_turn(session_key) is False
    assert runner._compaction_failures[session_key].count == 1
    skipped = [payload for _, payload in events if payload.get("status") == "skipped"]
    assert skipped[-1]["reason"] == "stale_preimage"
    assert skipped[-1]["applied"] is False
    assert skipped[-1]["durability"] == "none"
    assert skipped[-1]["user_visible"] is False

    class _HistoryCapture:
        provider = SimpleNamespace(provider_name="test")

        def __init__(self) -> None:
            self.history: list[Any] = []

        def set_history(self, history: list[Any]) -> None:
            self.history = history

    agent = _HistoryCapture()
    summary_context = await runner._load_history(agent, session_key, trim_last_user=False)
    assert summary_context is None
    assert len(agent.history) == len(entries)


@pytest.mark.asyncio
async def test_preflight_backfilled_flush_receipt_allows_compact() -> None:
    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    mock_sm = MagicMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)
    mock_sm.compact = AsyncMock(return_value="summary text")

    flush_service = MagicMock()
    flush_service.execute = AsyncMock(return_value=_flush_receipt(obligation_status="backfilled"))
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=mock_sm,
        session_flush_service=flush_service,
        config=_flush_enabled_config(),
    )

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact("agent:ops:long-session", context_window)

    await asyncio.sleep(0)
    flush_service.execute.assert_awaited_once()
    mock_sm.compact.assert_awaited_once_with("agent:ops:long-session", context_window)


@pytest.mark.asyncio
async def test_preflight_uses_background_timeout_for_flush_service() -> None:
    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    mock_sm = MagicMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)
    mock_sm.compact = AsyncMock(return_value="summary text")

    flush_service = MagicMock()
    flush_service.execute = AsyncMock(return_value=_flush_receipt())
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=mock_sm,
        session_flush_service=flush_service,
        config=SimpleNamespace(
            memory=SimpleNamespace(
                flush_enabled=True,
                flush_pre_compaction=True,
                flush_timeout_seconds=0.25,
                flush_background_timeout_seconds=42.0,
            )
        ),
    )

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact("agent:ops:long-session", context_window)

    await asyncio.sleep(0)
    assert flush_service.execute.await_args.kwargs["timeout"] == 42.0
    mock_sm.compact.assert_awaited_once_with("agent:ops:long-session", context_window)


@pytest.mark.asyncio
async def test_preflight_flush_grace_timeout_does_not_block_compaction() -> None:
    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    mock_sm = MagicMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)
    mock_sm.compact = AsyncMock(return_value="summary text")

    flush_service = MagicMock()

    async def slow_flush(*_args, **_kwargs):
        import asyncio

        await asyncio.sleep(0.05)
        return _flush_receipt()

    flush_service.execute = AsyncMock(side_effect=slow_flush)
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=mock_sm,
        session_flush_service=flush_service,
        config=SimpleNamespace(
            memory=SimpleNamespace(
                flush_enabled=True,
                flush_pre_compaction=True,
                flush_timeout_seconds=0.001,
                flush_background_timeout_seconds=42.0,
            )
        ),
    )

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact("agent:ops:long-session", context_window)

    await asyncio.sleep(0)
    assert flush_service.execute.await_args.kwargs["timeout"] == 42.0
    mock_sm.compact.assert_awaited_once_with("agent:ops:long-session", context_window)
    await asyncio.sleep(0.06)


@pytest.mark.asyncio
async def test_preflight_memory_flush_disabled_compacts_without_flush() -> None:
    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    mock_sm = MagicMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)
    mock_sm.compact = AsyncMock(return_value="summary text")

    flush_service = MagicMock()
    flush_service.execute = AsyncMock(return_value=_flush_receipt())
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=mock_sm,
        session_flush_service=flush_service,
        config=SimpleNamespace(
            memory=SimpleNamespace(flush_enabled=False, flush_timeout_seconds=0.25)
        ),
    )

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact("agent:ops:long-session", context_window)

    flush_service.execute.assert_not_called()
    mock_sm.compact.assert_awaited_once_with("agent:ops:long-session", context_window)


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
@pytest.mark.asyncio
async def test_preflight_env_flush_disabled_compacts_without_flush(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_SESSION_FLUSH", value)
    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    mock_sm = MagicMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)
    mock_sm.compact = AsyncMock(return_value="summary text")

    flush_service = MagicMock()
    flush_service.execute = AsyncMock(return_value=_flush_receipt())
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=mock_sm,
        session_flush_service=flush_service,
        config=SimpleNamespace(
            memory=SimpleNamespace(flush_enabled=True, flush_timeout_seconds=0.25)
        ),
    )

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact("agent:ops:long-session", context_window)

    flush_service.execute.assert_not_called()
    mock_sm.compact.assert_awaited_once_with("agent:ops:long-session", context_window)


@pytest.mark.asyncio
async def test_preflight_flush_service_unavailable_does_not_block_compaction() -> None:
    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    mock_sm = MagicMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)
    mock_sm.compact = AsyncMock(return_value="summary text")

    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=mock_sm,
        session_flush_service=None,
        config=SimpleNamespace(
            memory=SimpleNamespace(flush_enabled=True, flush_timeout_seconds=0.25)
        ),
    )

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact("agent:ops:long-session", context_window)

    mock_sm.compact.assert_awaited_once_with("agent:ops:long-session", context_window)


@pytest.mark.asyncio
async def test_preflight_passes_provider_backed_compaction_config_after_flush() -> None:
    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    captured_configs: list[CompactionConfig | None] = []

    mock_sm = MagicMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)

    async def compact(session_key, context_window_tokens, config=None):
        captured_configs.append(config)
        return "summary text"

    mock_sm.compact = AsyncMock(side_effect=compact)

    flush_service = MagicMock()
    flush_service.execute = AsyncMock(return_value=_flush_receipt())

    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=mock_sm,
        session_flush_service=flush_service,
        config=SimpleNamespace(
            compaction=SimpleNamespace(enabled=True, model=None, timeout_seconds=17.5)
        ),
    )
    provider = _FakeCompactionProvider(model="provider/model")

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact(
            "agent:ops:long-session",
            context_window,
            compaction_provider=provider,
            compaction_model="routed/model",
        )

    assert len(captured_configs) == 1
    config = captured_configs[0]
    assert isinstance(config, CompactionConfig)
    assert config.api_key == "preflight-provider-key"
    assert config.model == "routed/model"
    assert config.base_url == "https://openrouter.ai/api/v1"
    assert config.timeout_seconds == 17.5


@pytest.mark.asyncio
async def test_preflight_passes_profile_config_without_provider_or_model() -> None:
    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    sm = _ResultCompactionSessionManager(entries)
    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=sm,
        config=SimpleNamespace(
            compaction=SimpleNamespace(
                enabled=False,
                compaction_profile="coding",
                protected_recent_messages=7,
                timeout_seconds=17.5,
            )
        ),
    )

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact("agent:ops:long-session", context_window)

    assert len(sm.compact_with_result_calls) == 1
    config = sm.compact_with_result_calls[0][2]
    assert isinstance(config, CompactionConfig)
    assert config.model is None
    assert config.api_key == ""
    assert config.compaction_profile == "coding"
    assert config.protected_recent_messages == 7
    assert config.timeout_seconds == 17.5


@pytest.mark.asyncio
async def test_preflight_keeps_legacy_compact_manager_compatible() -> None:
    context_window = 1000
    entries = [_make_entry("early durable fact " + ("a" * 4000))]
    calls: list[tuple[str, int]] = []

    mock_sm = MagicMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)

    async def compact(session_key, context_window_tokens):
        calls.append((session_key, context_window_tokens))
        return "summary text"

    mock_sm.compact = AsyncMock(side_effect=compact)

    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=mock_sm,
        config=SimpleNamespace(
            compaction=SimpleNamespace(enabled=True, model=None, timeout_seconds=17.5)
        ),
    )

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact(
            "agent:ops:long-session",
            context_window,
            compaction_provider=_FakeCompactionProvider(model="provider/model"),
            compaction_model="routed/model",
        )

    assert calls == [("agent:ops:long-session", context_window)]


@pytest.mark.asyncio
async def test_run_falls_back_to_generic_preflight_after_t3_flush_failed() -> None:
    selector = _FakeProviderSelector()
    runner = TurnRunner(provider_selector=selector, config=GatewayConfig())
    seen: dict[str, object] = {}

    async def fake_t3(session_key, turn, context_window_tokens, **kwargs):
        seen["t3_session_key"] = session_key
        return "flush_failed"

    async def spy_preflight(session_key, context_window_tokens, **kwargs):
        seen["preflight_session_key"] = session_key
        seen.update(kwargs)

    runner._maybe_compact_on_t3_upgrade = fake_t3  # type: ignore[method-assign]
    runner._maybe_preflight_compact = spy_preflight  # type: ignore[method-assign]
    tool_ctx = ToolContext(is_owner=True, caller_kind=CallerKind.CLI)

    async for _ in runner.run(
        "hello",
        "agent:main:abc123",
        tool_context=tool_ctx,
        model="routed/model",
    ):
        pass

    assert seen["t3_session_key"] == "agent:main:abc123"
    assert seen["preflight_session_key"] == "agent:main:abc123"
    assert seen["compaction_model"] == "routed/model"


@pytest.mark.asyncio
async def test_run_forwards_routed_provider_and_model_to_preflight() -> None:
    selector = _FakeProviderSelector()
    runner = TurnRunner(
        provider_selector=selector,
        config=GatewayConfig(),
        model_catalog=ModelCatalog(),
    )
    seen: dict[str, object] = {}

    async def spy_preflight(session_key, context_window_tokens, **kwargs):
        seen["session_key"] = session_key
        seen["context_window_tokens"] = context_window_tokens
        seen.update(kwargs)

    runner._maybe_preflight_compact = spy_preflight  # type: ignore[method-assign]
    tool_ctx = ToolContext(is_owner=True, caller_kind=CallerKind.CLI)

    async for _ in runner.run(
        "hello",
        "agent:main:abc123",
        tool_context=tool_ctx,
        model="z-ai/glm-5.1",
    ):
        pass

    assert seen["session_key"] == "agent:main:abc123"
    # Conservative-min static fallback for glm-5.1 (formerly 202_752 under the
    # z-ai/ spelling; merged with the bare glm-5.1 entry's 200_000 window).
    assert seen["context_window_tokens"] == 200_000
    assert seen["compaction_model"] == "z-ai/glm-5.1"
    assert getattr(seen["compaction_provider"], "model") == "z-ai/glm-5.1"
    assert selector.override_calls == []
    assert selector.clone_instance.override_calls[-1] == "z-ai/glm-5.1"


@pytest.mark.asyncio
async def test_preflight_compact_called_with_correct_args() -> None:
    """compact() is called with (session_key, context_window_tokens)."""
    entries = [_make_entry("content")]

    mock_sm = MagicMock()
    mock_sm.compact = AsyncMock(return_value="")
    mock_sm.get_transcript = AsyncMock(return_value=entries)

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=mock_sm)

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=90_000):
        await runner._maybe_preflight_compact("user:long-session", 100_000)

    mock_sm.compact.assert_called_once_with("user:long-session", 100_000)


@pytest.mark.asyncio
async def test_preflight_exactly_at_threshold_does_not_compact() -> None:
    """Transcript at exactly the threshold (not exceeding) → no compaction."""
    context_window = 1000
    threshold = int(context_window * 0.85)  # default threshold: 850

    entries = [_make_entry("a")]

    mock_sm = MagicMock()
    mock_sm.compact = AsyncMock()
    mock_sm.get_transcript = AsyncMock(return_value=entries)

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=mock_sm)

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=threshold):
        await runner._maybe_preflight_compact("user:session", context_window)

    mock_sm.compact.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_integration_with_real_session_manager(session_mgr, tmp_path) -> None:
    """Integration: pre-flight with real SessionManager compacts when over threshold."""
    mgr = session_mgr
    key = "user:preflight-test"
    await mgr.create(key)

    # Seed transcript entries
    await mgr.append_message(key, role="user", content="message one")
    await mgr.append_message(key, role="assistant", content="reply one")
    await mgr.append_message(key, role="user", content="message two")

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=mgr)

    # Patch compact_with_result() to verify the metadata-preserving path is used.
    original_compact_with_result = mgr.compact_with_result
    compact_calls: list[dict[str, object]] = []

    async def _spy_compact_with_result(
        session_key,
        context_window_tokens,
        config=None,
        *,
        compaction_id=None,
        trigger_reason=None,
        flush_receipt_status=None,
    ):
        compact_calls.append(
            {
                "session_key": session_key,
                "context_window_tokens": context_window_tokens,
                "compaction_id": compaction_id,
                "trigger_reason": trigger_reason,
                "flush_receipt_status": flush_receipt_status,
            }
        )
        return await original_compact_with_result(
            session_key,
            context_window_tokens,
            config,
            compaction_id=compaction_id,
            trigger_reason=trigger_reason,
            flush_receipt_status=flush_receipt_status,
        )

    mgr.compact_with_result = _spy_compact_with_result  # type: ignore[method-assign]

    # Force all tokens to exceed the default threshold (100 * 0.85 = 85)
    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        await runner._maybe_preflight_compact(key, 100)

    # compact_with_result() was invoked with the correct args.
    assert len(compact_calls) == 1
    assert compact_calls[0]["session_key"] == key
    assert compact_calls[0]["context_window_tokens"] == 100
    assert compact_calls[0]["compaction_id"]
    assert compact_calls[0]["trigger_reason"] == "preflight"
    assert compact_calls[0]["flush_receipt_status"] == "not_required"
    receipts = await mgr.storage.list_memory_durable_receipts(
        session_key=key,
        status="checkpoint_saved",
        limit=1,
    )
    assert receipts
    assert receipts[0].scope == "checkpoint"
    assert receipts[0].status == "checkpoint_saved"
    assert receipts[0].source_path
    assert (tmp_path / receipts[0].source_path).exists()


@pytest.mark.asyncio
async def test_preflight_compaction_circuit_breaker_retries_after_cooldown() -> None:
    context_window = 1000
    entries = [_make_entry("a" * 4000)]

    mock_sm = MagicMock()
    mock_sm.compact = AsyncMock(side_effect=RuntimeError("compact failed"))
    mock_sm.get_transcript = AsyncMock(return_value=entries)

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=mock_sm)

    with patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000):
        for _ in range(4):
            await runner._maybe_preflight_compact("user:session", context_window)
            runner.clear_compaction_turn_state("user:session")

    assert mock_sm.compact.await_count == 3

    runner._compaction_failures["user:session"].opened_at = 0.0
    with (
        patch("opensquilla.session.tokenizer.estimate_tokens", return_value=1000),
        patch("opensquilla.engine.runtime.time.monotonic", return_value=999.0),
    ):
        await runner._maybe_preflight_compact("user:session", context_window)

    assert mock_sm.compact.await_count == 4
