"""Tests for TurnRunner._maybe_compact_on_t3_upgrade()."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from opensquilla.engine import runtime as runtime_module
from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.runtime import TurnRunner
from opensquilla.session import compaction as compaction_module
from opensquilla.session.compaction import CompactionConfig
from opensquilla.session.models import TranscriptEntry

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSessionManager:
    def __init__(self, transcript: list[TranscriptEntry] | None = None) -> None:
        self._transcript = transcript or []
        self.compact_calls: list[tuple[str, int]] = []
        self.compact_configs: list[object | None] = []

    async def get_transcript(self, session_key: str, **kwargs: Any) -> list[TranscriptEntry]:
        return list(self._transcript)

    async def compact(self, session_key: str, context_window_tokens: int, **kwargs: Any) -> str:
        self.compact_calls.append((session_key, context_window_tokens))
        return "summary"


class _ResultCompactionSessionManager(_FakeSessionManager):
    async def compact_with_result(
        self,
        session_key: str,
        context_window_tokens: int,
        config: object | None = None,
    ) -> SimpleNamespace:
        self.compact_calls.append((session_key, context_window_tokens))
        self.compact_configs.append(config)
        return SimpleNamespace(
            summary="summary",
            kept_entries=[{"role": "assistant", "content": "tail"}],
            removed_count=2,
            chunks_processed=1,
            summary_source="llm",
            tokens_before=300,
            tokens_after=100,
            remaining_budget_tokens=context_window_tokens - 100,
        )


class _StaleResultCompactionSessionManager(_FakeSessionManager):
    async def compact_with_result(
        self,
        session_key: str,
        context_window_tokens: int,
        config: object | None = None,
        **kwargs: Any,
    ) -> SimpleNamespace:
        self.compact_calls.append((session_key, context_window_tokens))
        self.compact_configs.append(config)
        return SimpleNamespace(
            summary="",
            kept_entries=list(self._transcript),
            removed_count=0,
            chunks_processed=0,
            summary_source="skipped",
            skip_reason="stale_preimage",
            tokens_before=300,
            tokens_after=300,
            remaining_budget_tokens=context_window_tokens - 300,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_transcript() -> list[TranscriptEntry]:
    return [
        TranscriptEntry(
            session_id="s1",
            session_key="agent:main:webchat:default",
            role="user",
            content="hello",
            token_count=60_000,
        ),
        TranscriptEntry(
            session_id="s1",
            session_key="agent:main:webchat:default",
            role="assistant",
            content="hi there",
            token_count=60_000,
        ),
    ]


def _within_budget_transcript() -> list[TranscriptEntry]:
    return [
        TranscriptEntry(
            session_id="s1",
            session_key="agent:main:webchat:default",
            role="user",
            content="hello",
            token_count=10,
        ),
        TranscriptEntry(
            session_id="s1",
            session_key="agent:main:webchat:default",
            role="assistant",
            content="hi there",
            token_count=10,
        ),
    ]


def _tool_heavy_transcript() -> list[TranscriptEntry]:
    line = "drwxr-xr-x staff 4096 synthetic/file.txt "
    result_text = (line * 50)[:2000]
    entries: list[TranscriptEntry] = []
    for turn in range(10):
        tool_calls: list[dict[str, Any]] = []
        for pair in range(4):
            tool_id = f"tool-{turn}-{pair}"
            tool_calls.append(
                {
                    "type": "tool_use",
                    "tool_use_id": tool_id,
                    "name": "exec_shell",
                    "input": {"command": f"ls batch_{turn}/{pair}"},
                }
            )
            tool_calls.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "name": "exec_shell",
                    "result": result_text,
                    "is_error": False,
                }
            )
        entries.append(
            TranscriptEntry(
                session_id="s1",
                session_key="agent:main:webchat:default",
                role="assistant",
                content=f"inspected batch {turn}",
                token_count=120,
                tool_calls=tool_calls,
            )
        )
    return entries


def _make_turn(
    routed_tier: str = "c3",
    previous_tier: str | None = "c2",
    base_tier: str | None = None,
    final_tier: str | None = None,
    routing_applied: bool = True,
) -> TurnContext:
    routing_extra: dict[str, Any] = {}
    if previous_tier is not None:
        routing_extra["previous_tier"] = previous_tier
    if base_tier is not None:
        routing_extra["base_tier"] = base_tier
    if final_tier is not None:
        routing_extra["final_tier"] = final_tier

    return TurnContext(
        message="test",
        session_key="agent:main:webchat:default",
        config=None,
        provider=None,
        model="anthropic/claude-opus-4.8",
        tool_defs=[],
        system_prompt="you are helpful",
        metadata={
            "routed_tier": routed_tier,
            "routing_applied": routing_applied,
            "routing_extra": routing_extra,
        },
    )


def _make_runner(
    session_manager: Any = None,
    enabled: bool = True,
    *,
    compaction_config: Any = None,
) -> TurnRunner:
    config = SimpleNamespace(
        squilla_router=SimpleNamespace(upgrade_to_c3_compaction_enabled=enabled),
        compaction=compaction_config,
        memory=SimpleNamespace(),
    )
    return TurnRunner(
        provider_selector=SimpleNamespace(clone=lambda: SimpleNamespace()),
        session_manager=session_manager,
        config=config,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_to_t3_triggers_compaction() -> None:
    sm = _FakeSessionManager(_sample_transcript())
    runner = _make_runner(session_manager=sm)

    turn = _make_turn(routed_tier="c3", previous_tier="c2")
    result = await runner._maybe_compact_on_t3_upgrade("agent:main:webchat:default", turn, 100_000)

    assert result == "handled"
    await asyncio.sleep(0)
    assert len(sm.compact_calls) == 1
    assert sm.compact_calls[0] == ("agent:main:webchat:default", 100_000)


@pytest.mark.asyncio
async def test_t3_passes_profile_config_without_provider_or_model() -> None:
    sm = _ResultCompactionSessionManager(_sample_transcript())
    runner = _make_runner(
        session_manager=sm,
        compaction_config=SimpleNamespace(
            enabled=False,
            compaction_profile="coding",
            protected_recent_messages=5,
            timeout_seconds=11.0,
        ),
    )

    turn = _make_turn(routed_tier="t3", previous_tier="t2")
    result = await runner._maybe_compact_on_t3_upgrade(
        "agent:main:webchat:default",
        turn,
        100_000,
    )

    assert result == "handled"
    config = sm.compact_configs[0]
    assert isinstance(config, CompactionConfig)
    assert config.model is None
    assert config.api_key == ""
    assert config.compaction_profile == "coding"
    assert config.protected_recent_messages == 5
    assert config.timeout_seconds == 11.0


@pytest.mark.asyncio
async def test_t3_protects_active_and_queued_prompts_in_compaction_config() -> None:
    session_key = "agent:main:webchat:default"
    active_user = TranscriptEntry(
        session_id="s1",
        session_key=session_key,
        role="user",
        content="active user",
        token_count=10,
    )
    transcript = [
        TranscriptEntry(
            session_id="s1",
            session_key=session_key,
            role="user",
            content="old user",
            token_count=60_000,
        ),
        TranscriptEntry(
            session_id="s1",
            session_key=session_key,
            role="assistant",
            content="old assistant",
            token_count=60_000,
        ),
        active_user,
        TranscriptEntry(
            session_id="s1",
            session_key=session_key,
            role="user",
            content="queued user",
            token_count=10,
        ),
    ]
    sm = _ResultCompactionSessionManager(transcript)
    runner = _make_runner(session_manager=sm)

    result = await runner._maybe_compact_on_t3_upgrade(
        session_key,
        _make_turn(),
        100_000,
        history_has_persisted_user=True,
        bound_user_message_id=active_user.message_id,
    )

    assert result == "handled"
    assert len(sm.compact_configs) == 1
    config = sm.compact_configs[0]
    assert isinstance(config, CompactionConfig)
    assert config.protected_recent_messages == 2


@pytest.mark.asyncio
async def test_t3_skips_durable_work_when_active_prompt_alone_is_too_large() -> None:
    session_key = "agent:main:webchat:default"
    active_user = TranscriptEntry(
        session_id="s1",
        session_key=session_key,
        role="user",
        content="active user",
        token_count=1000,
    )
    transcript = [
        TranscriptEntry(
            session_id="s1",
            session_key=session_key,
            role="assistant",
            content="small old history",
            token_count=10,
        ),
        active_user,
    ]
    sm = _ResultCompactionSessionManager(transcript)
    sm.record_memory_checkpoint = AsyncMock()
    runner = _make_runner(session_manager=sm)

    result = await runner._maybe_compact_on_t3_upgrade(
        session_key,
        _make_turn(),
        1000,
        history_has_persisted_user=True,
        bound_user_message_id=active_user.message_id,
    )

    assert result == "handled"
    assert sm.compact_calls == []
    sm.record_memory_checkpoint.assert_not_called()


@pytest.mark.asyncio
async def test_t3_within_budget_skips_compaction() -> None:
    sm = _FakeSessionManager(_within_budget_transcript())
    runner = _make_runner(session_manager=sm)

    turn = _make_turn(routed_tier="c3", previous_tier="c2")
    result = await runner._maybe_compact_on_t3_upgrade("agent:main:webchat:default", turn, 100_000)

    assert result == "handled"
    await asyncio.sleep(0)
    assert sm.compact_calls == []


@pytest.mark.asyncio
async def test_t3_budget_check_counts_full_tool_call_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    monkeypatch.setattr(
        compaction_module,
        "_estimate_tokens",
        lambda text: max(1, len(text) // 4),
    )
    transcript = _tool_heavy_transcript()
    summarized = sum(compaction_module.estimate_entry_replay_tokens(e) for e in transcript)
    model_replay = sum(
        compaction_module.estimate_entry_model_replay_tokens(e) for e in transcript
    )
    # Derive the window from the estimators instead of hard-coding a token
    # count. This assertion is about WHICH estimator the budget check consults,
    # not about an incidental absolute token count. The midpoint of the valid
    # band keeps margin on both sides while deterministic token math keeps the
    # test offline.
    safety_margin = 1.2
    window = int((summarized + model_replay) * safety_margin / 2)
    # The summarized estimate looks within budget while the model replay overflows.
    assert summarized * safety_margin <= window < model_replay * safety_margin

    sm = _FakeSessionManager(transcript)
    runner = _make_runner(session_manager=sm)

    turn = _make_turn(routed_tier="c3", previous_tier="c2")
    result = await runner._maybe_compact_on_t3_upgrade("agent:main:webchat:default", turn, window)

    assert result == "handled"
    await asyncio.sleep(0)
    assert sm.compact_calls == [("agent:main:webchat:default", window)]


@pytest.mark.asyncio
async def test_t3_completed_event_reports_compaction_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sm = _ResultCompactionSessionManager(_sample_transcript())
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        runtime_module,
        "notify_compaction",
        lambda session_key, **payload: events.append((session_key, payload)),
    )
    runner = _make_runner(session_manager=sm)

    turn = _make_turn(routed_tier="c3", previous_tier="c2")
    result = await runner._maybe_compact_on_t3_upgrade("agent:main:webchat:default", turn, 100_000)

    assert result == "handled"
    assert [(key, payload["status"]) for key, payload in events] == [
        ("agent:main:webchat:default", "started"),
        ("agent:main:webchat:default", "observed"),
        ("agent:main:webchat:default", "observed"),
        ("agent:main:webchat:default", "completed"),
    ]
    compaction_ids = {payload.get("compaction_id") for _, payload in events}
    assert len(compaction_ids) == 1
    assert None not in compaction_ids
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
    assert completed["chunk_count"] == 1
    assert completed["summary_source"] == "llm"
    assert completed["removed_count"] == 2
    assert completed["kept_count"] == 1
    assert completed["tokens_after"] == 100
    assert completed["remaining_budget_tokens"] == 99_900


@pytest.mark.asyncio
async def test_t3_stale_preimage_skip_does_not_mark_compacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sm = _StaleResultCompactionSessionManager(_sample_transcript())
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        runtime_module,
        "notify_compaction",
        lambda session_key, **payload: events.append((session_key, payload)),
    )
    runner = _make_runner(session_manager=sm)
    session_key = "agent:main:webchat:default"

    turn = _make_turn(routed_tier="c3", previous_tier="c2")
    result = await runner._maybe_compact_on_t3_upgrade(session_key, turn, 100_000)

    assert result == "handled"
    assert sm.compact_calls == [(session_key, 100_000)]
    assert runner.has_compacted_this_turn(session_key) is False
    skipped = [payload for _, payload in events if payload.get("status") == "stale"]
    assert skipped[-1]["reason"] == "stale_preimage"
    assert skipped[-1]["applied"] is False
    assert skipped[-1]["durability"] == "none"
    assert skipped[-1]["user_visible"] is False


@pytest.mark.asyncio
async def test_t0_t1_to_t3_triggers() -> None:
    for prev in ("c0", "c1"):
        sm = _FakeSessionManager(_sample_transcript())
        runner = _make_runner(session_manager=sm)

        turn = _make_turn(routed_tier="c3", previous_tier=prev)
        result = await runner._maybe_compact_on_t3_upgrade(
            "agent:main:webchat:default", turn, 100_000
        )

        assert result == "handled", f"failed for previous_tier={prev}"
        await asyncio.sleep(0)
        assert len(sm.compact_calls) == 1


@pytest.mark.asyncio
async def test_t3_to_t3_skips() -> None:
    sm = _FakeSessionManager(_sample_transcript())
    runner = _make_runner(session_manager=sm)

    turn = _make_turn(routed_tier="c3", previous_tier="c3")
    result = await runner._maybe_compact_on_t3_upgrade("agent:main:webchat:default", turn, 100_000)

    assert result == "not_applicable"
    assert len(sm.compact_calls) == 0


@pytest.mark.asyncio
async def test_non_t3_route_skips() -> None:
    sm = _FakeSessionManager(_sample_transcript())
    runner = _make_runner(session_manager=sm)

    turn = _make_turn(routed_tier="c1", previous_tier="c0")
    result = await runner._maybe_compact_on_t3_upgrade("agent:main:webchat:default", turn, 100_000)

    assert result == "not_applicable"
    assert len(sm.compact_calls) == 0


@pytest.mark.asyncio
async def test_config_disabled_skips() -> None:
    sm = _FakeSessionManager(_sample_transcript())
    runner = _make_runner(session_manager=sm,  enabled=False)

    turn = _make_turn(routed_tier="c3", previous_tier="c2")
    result = await runner._maybe_compact_on_t3_upgrade("agent:main:webchat:default", turn, 100_000)

    assert result == "not_applicable"
    assert len(sm.compact_calls) == 0


@pytest.mark.asyncio
async def test_observe_mode_skips() -> None:
    sm = _FakeSessionManager(_sample_transcript())
    runner = _make_runner(session_manager=sm)

    turn = _make_turn(routed_tier="c3", previous_tier="c2", routing_applied=False)
    result = await runner._maybe_compact_on_t3_upgrade("agent:main:webchat:default", turn, 100_000)

    assert result == "not_applicable"
    assert len(sm.compact_calls) == 0


@pytest.mark.asyncio
async def test_compact_raises_continues() -> None:
    sm = _FakeSessionManager(_sample_transcript())

    async def _boom(session_key: str, context_window_tokens: int, **kw: Any) -> str:
        raise RuntimeError("compact boom")

    sm.compact = _boom  # type: ignore[assignment]
    runner = _make_runner(session_manager=sm)

    turn = _make_turn(routed_tier="c3", previous_tier="c2")
    result = await runner._maybe_compact_on_t3_upgrade("agent:main:webchat:default", turn, 100_000)

    assert result == "compact_failed"
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_t3_compact_failure_uses_emergency_ephemeral_history_trim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "agent:main:webchat:t3-emergency"
    transcript = [
        TranscriptEntry(
            session_id="s1",
            session_key=session_key,
            role="user" if index % 2 == 0 else "assistant",
            content=f"historic t3 message {index} " + ("x" * 500),
            token_count=300,
        )
        for index in range(8)
    ]
    sm = _FakeSessionManager(transcript)

    async def _boom(session_key: str, context_window_tokens: int, **kw: Any) -> str:
        raise RuntimeError("compact boom")

    sm.compact = _boom  # type: ignore[assignment]
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        runtime_module,
        "notify_compaction",
        lambda session_key, **payload: events.append((session_key, payload)),
    )
    runner = _make_runner(session_manager=sm)

    result = await runner._maybe_compact_on_t3_upgrade(
        session_key,
        _make_turn(routed_tier="c3", previous_tier="c2"),
        1000,
    )

    class _HistoryCapture:
        provider = SimpleNamespace(provider_name="test")

        def __init__(self) -> None:
            self.history: list[Any] = []

        def set_history(self, history: list[Any]) -> None:
            self.history = history

        def set_request_image_context(self, messages: list[Any]) -> None:
            assert messages == []

    agent = _HistoryCapture()
    summary_context = await runner._load_history(agent, session_key, trim_last_user=False)

    assert result == "compact_failed"
    assert len(await sm.get_transcript(session_key)) == len(transcript)
    assert len(agent.history) < len(transcript)
    assert summary_context is not None
    assert "temporary history window" in summary_context.lower()
    statuses = [payload["status"] for _, payload in events]
    assert statuses[:2] == ["started", "emergency_ephemeral"]
    assert "failed" not in statuses
    emergency = next(payload for _, payload in events if payload["status"] == "emergency_ephemeral")
    assert emergency["durability"] == "request_scoped"
    assert runner._compaction_failures[session_key].count == 1


@pytest.mark.asyncio
async def test_t3_open_circuit_still_uses_request_scoped_emergency_trim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "agent:main:webchat:t3-open-circuit"
    transcript = [
        TranscriptEntry(
            session_id="s1",
            session_key=session_key,
            role="user" if index % 2 == 0 else "assistant",
            content=f"historic t3 message {index} " + ("x" * 500),
            token_count=300,
        )
        for index in range(8)
    ]
    sm = _FakeSessionManager(transcript)
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        runtime_module,
        "notify_compaction",
        lambda session_key, **payload: events.append((session_key, payload)),
    )
    runner = _make_runner(session_manager=sm)
    runner._compaction_failures[session_key] = runtime_module._CompactionFailureState(
        count=3,
        opened_at=runtime_module.time.monotonic(),
    )
    from opensquilla.engine import request_window

    selected_windows: list[int] = []
    original_cuts = request_window.iter_window_prefix_cuts

    def observe_cuts(roles, **kwargs):
        selected_windows.append(kwargs["protected_start"])
        return original_cuts(roles, **kwargs)

    monkeypatch.setattr(request_window, "iter_window_prefix_cuts", observe_cuts)

    result = await runner._maybe_compact_on_t3_upgrade(
        session_key,
        _make_turn(routed_tier="c3", previous_tier="c2"),
        10_000,
        history_capacity_tokens=1_500,
        history_capacity_chars=5_000,
    )

    assert result == "handled"
    assert sm.compact_calls == []
    assert [payload["status"] for _, payload in events] == ["emergency_ephemeral"]
    emergency = events[-1][1]
    assert emergency["reason"] == "durable_compaction_circuit_open"
    assert emergency["durability"] == "request_scoped"
    assert runner._compaction_failures[session_key].count == 3
    assert selected_windows == [len(transcript) - 2]
    override = runner._emergency_compaction_overrides[session_key]
    assert override.history_window_tokens == 1_500
    assert override.history_capacity_chars == 5_000
