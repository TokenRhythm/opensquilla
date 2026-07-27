"""Tests for the context-overflow policy branches."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway import context_overflow
from opensquilla.gateway.config import ContextOverflowPolicy, GatewayConfig
from opensquilla.gateway.context_overflow import (
    OverflowOutcome,
    apply_context_overflow_policy,
)
from opensquilla.gateway.rpc_chat import _enforce_context_overflow, _handle_chat_send
from opensquilla.provider.types import ProviderRequestCorrelation
from opensquilla.session.compaction import CompactionConfig
from opensquilla.session.compaction_state import (
    StructuredCompactionSummary,
    render_structured_summary,
)
from opensquilla.session.models import SessionContextState, SessionSummary
from opensquilla.session.tokenizer import estimate_tokens


@dataclass
class _FakeEntry:
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    reasoning_content: str | None = None
    token_count: int | None = None


class _FakeSessionManager:
    """Minimal session-manager stub: tracks compact() calls + transcript."""

    def __init__(self, transcript: list[_FakeEntry]) -> None:
        self._transcript = list(transcript)
        self.compact_calls: list[tuple[str, int, object | None]] = []
        self.compact_kwargs: list[dict[str, Any]] = []

    async def get_transcript(self, session_key: str) -> list[_FakeEntry]:
        return list(self._transcript)

    async def compact(self, session_key: str, budget: int, config=None) -> str:
        # Simulate a successful compaction: collapse history into a single
        # short summary entry so the next estimate fits easily.
        self.compact_calls.append((session_key, budget, config))
        self._transcript = [_FakeEntry(content="[summary]")]
        return "[summary]"


class _ResultCompactionSessionManager(_FakeSessionManager):
    async def compact_with_result(self, session_key: str, budget: int, config=None, **kwargs):
        self.compact_calls.append((session_key, budget, config))
        self.compact_kwargs.append(dict(kwargs))
        self._transcript = [_FakeEntry(content="[summary]")]
        return SimpleNamespace(
            summary="[summary]",
            kept_entries=[{"role": "assistant", "content": "[tail]"}],
            removed_count=5,
            chunks_processed=2,
            summary_source="llm",
            tokens_before=900,
            tokens_after=90,
            remaining_budget_tokens=max(budget - 90, 0),
        )


class _InsufficientResultCompactionSessionManager(_FakeSessionManager):
    async def compact_with_result(self, session_key: str, budget: int, config=None, **kwargs):
        self.compact_calls.append((session_key, budget, config))
        self.compact_kwargs.append(dict(kwargs))
        self._transcript = [_FakeEntry(content="[still too large]", token_count=100)]
        return SimpleNamespace(
            summary="[summary]",
            kept_entries=[{"role": "assistant", "content": "[still too large]"}],
            removed_count=5,
            chunks_processed=2,
            summary_source="llm",
            tokens_before=900,
            tokens_after=100,
            remaining_budget_tokens=0,
        )


class _InsufficientCompactionSessionManager(_FakeSessionManager):
    async def compact(self, session_key: str, budget: int, config=None) -> str:
        self.compact_calls.append((session_key, budget, config))
        return "[summary]"


class _FailingCompactionSessionManager(_FakeSessionManager):
    async def compact(self, session_key: str, budget: int, config=None) -> str:
        self.compact_calls.append((session_key, budget, config))
        raise RuntimeError("compact boom")


class _LegacyCompactSessionManager(_FakeSessionManager):
    async def compact(self, session_key: str, budget: int) -> str:
        self.compact_calls.append((session_key, budget, None))
        self._transcript = [_FakeEntry(content="[summary]")]
        return "[summary]"


class _CheckpointingSessionManager(_FakeSessionManager):
    def __init__(self, transcript: list[_FakeEntry]) -> None:
        super().__init__(transcript)
        self.calls: list[str] = []

    async def record_memory_checkpoint(
        self,
        session_key: str,
        transcript: list[_FakeEntry],
        **kwargs,
    ) -> SimpleNamespace:
        self.calls.append("checkpoint")
        return SimpleNamespace(
            scope="checkpoint",
            status="checkpoint_saved",
            source_path="memory/.checkpoints/s/turn.jsonl",
            content_hash="h1",
        )

    async def compact(self, session_key: str, budget: int, config=None) -> str:
        self.calls.append("compact")
        return await super().compact(session_key, budget, config)


class _InvalidCheckpointSessionManager(_CheckpointingSessionManager):
    async def record_memory_checkpoint(
        self,
        session_key: str,
        transcript: list[_FakeEntry],
        **kwargs,
    ) -> SimpleNamespace:
        self.calls.append("checkpoint")
        return SimpleNamespace(
            scope="checkpoint",
            status="checkpoint_failed",
            source_path="memory/.checkpoints/s/turn.jsonl",
            content_hash="h1",
        )


class _FailingCheckpointSessionManager(_CheckpointingSessionManager):
    async def record_memory_checkpoint(
        self,
        session_key: str,
        transcript: list[_FakeEntry],
        **kwargs,
    ) -> None:
        self.calls.append("checkpoint")
        raise RuntimeError("checkpoint write failed")


class _SummaryReadFailureSessionManager(_FakeSessionManager):
    async def get_summaries(self, session_key: str) -> list[Any]:
        raise RuntimeError(f"summary store unavailable for {session_key}")


class _StructuredContextSessionManager(_FakeSessionManager):
    def __init__(
        self,
        transcript: list[_FakeEntry],
        *,
        summaries: list[SessionSummary],
        context_states: list[SessionContextState],
    ) -> None:
        super().__init__(transcript)
        self._summaries = list(summaries)
        self._context_states = list(context_states)

    async def get_summaries(self, session_key: str) -> list[SessionSummary]:
        return list(self._summaries)

    async def get_context_states(self, session_key: str) -> list[SessionContextState]:
        return list(self._context_states)


class _FakeCompactionProvider:
    provider_name = "openai"

    def __init__(self) -> None:
        self._api_key = "overflow-provider-key"
        self._model = "provider/model"
        self._base_url = "https://openrouter.ai/api/v1"

    @property
    def model(self) -> str:
        return self._model


class _FakeSelectorClone:
    def __init__(self, provider: _FakeCompactionProvider) -> None:
        self.provider = provider
        self.override_calls: list[str] = []

    def override_model(self, model: str) -> None:
        self.override_calls.append(model)
        self.provider._model = model

    def resolve(self) -> _FakeCompactionProvider:
        return self.provider


class _FakeProviderSelector:
    def __init__(self) -> None:
        self.provider = _FakeCompactionProvider()
        self.clone_instance = _FakeSelectorClone(self.provider)
        self.override_calls: list[str] = []

    def clone(self) -> _FakeSelectorClone:
        return self.clone_instance

    def override_model(self, model: str) -> None:
        self.override_calls.append(model)

    def resolve(self) -> _FakeCompactionProvider:
        return self.provider


class _TurnCompactionMarker:
    def __init__(self, compacted: set[str] | None = None) -> None:
        self.compacted = set(compacted or set())
        self.mark_calls: list[str] = []
        self.clear_calls: list[str] = []

    def has_compacted_this_turn(self, session_key: str) -> bool:
        return session_key in self.compacted

    def mark_compacted_this_turn(self, session_key: str) -> None:
        self.mark_calls.append(session_key)
        self.compacted.add(session_key)

    def clear_compacted_this_turn(self, session_key: str) -> None:
        self.clear_calls.append(session_key)
        self.compacted.discard(session_key)


class _FailingTurnCompactionMarker:
    def has_compacted_this_turn(self, session_key: str) -> bool:
        raise RuntimeError(f"marker unavailable for {session_key}")


def _cfg(
    policy: ContextOverflowPolicy,
    budget: int = 20,
    *,
    flush_enabled: bool = False,
    flush_timeout_seconds: float = 5.0,
    flush_background_timeout_seconds: float = 60.0,
    flush_pre_compaction: bool | None = None,
    flush_compaction_requires_safe_receipt: bool = False,
    flush_compaction_safety_mode: str | None = None,
) -> GatewayConfig:
    memory: dict[str, object] = {
        "flush_enabled": flush_enabled,
        "flush_timeout_seconds": flush_timeout_seconds,
        "flush_background_timeout_seconds": flush_background_timeout_seconds,
        "flush_compaction_requires_safe_receipt": (flush_compaction_requires_safe_receipt),
    }
    if flush_pre_compaction is None:
        flush_pre_compaction = flush_enabled
    memory["flush_pre_compaction"] = flush_pre_compaction
    if flush_compaction_safety_mode is not None:
        memory["flush_compaction_safety_mode"] = flush_compaction_safety_mode
    return GatewayConfig(
        context_overflow_policy=policy,
        context_budget_tokens=budget,
        memory=memory,
    )


def test_gateway_memory_flush_triggers_normalize_aliases() -> None:
    string_cfg = GatewayConfig(memory={"flush_triggers": "reset, inline_overflow"})
    list_cfg = GatewayConfig(memory={"flush_triggers": ["manual", "pre-compaction"]})

    assert string_cfg.memory.flush_triggers == ["session_reset", "pre_compaction"]
    assert list_cfg.memory.flush_triggers == ["manual", "pre_compaction"]


@pytest.mark.parametrize(
    "flush_triggers",
    [
        "bogus",
        ["manual", "bogus"],
    ],
)
def test_gateway_memory_flush_triggers_reject_unknown_values(
    flush_triggers: object,
) -> None:
    with pytest.raises(ValueError, match="unknown flush trigger"):
        GatewayConfig(memory={"flush_triggers": flush_triggers})


def _history(n_entries: int, chars_per_entry: int) -> list[_FakeEntry]:
    # estimate_tokens rounds chars/4, so ~4 chars ≈ 1 token.
    return [_FakeEntry(content="x" * chars_per_entry) for _ in range(n_entries)]


@pytest.mark.asyncio
async def test_default_policy_is_auto_summarize() -> None:
    """GatewayConfig default policy must be AUTO_SUMMARIZE per S4 AC."""

    cfg = GatewayConfig()
    assert cfg.context_overflow_policy == ContextOverflowPolicy.AUTO_SUMMARIZE
    assert cfg.context_budget_tokens == 100_000


@pytest.mark.asyncio
async def test_policy_enum_has_exactly_three_members() -> None:
    """Locks S4 AC: exactly three policy options, stable string values."""

    values = {m.value for m in ContextOverflowPolicy}
    assert values == {"auto_summarize", "hard_truncate", "refuse"}


@pytest.mark.asyncio
async def test_under_budget_is_noop() -> None:
    cfg = _cfg(ContextOverflowPolicy.REFUSE, budget=10_000)
    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="hi",
        transcript=_history(1, 4),
        session_key="s1",
    )
    assert outcome.over_budget is False
    assert outcome.refusal is None


@pytest.mark.asyncio
async def test_refuse_returns_stable_error_envelope() -> None:
    """REFUSE short-circuits with the documented error envelope."""

    cfg = _cfg(ContextOverflowPolicy.REFUSE, budget=5)
    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="hello",
        transcript=_history(4, 40),
        session_key="s-refuse",
    )
    assert outcome.over_budget is True
    assert outcome.refusal is not None
    env = outcome.refusal
    assert env["status"] == "error"
    assert env["error_class"] == "context_overflow"
    assert env["retry_allowed"] is False
    assert isinstance(env["user_message"], str) and env["user_message"]


@pytest.mark.asyncio
async def test_gateway_context_overflow_counts_tool_call_arguments() -> None:
    cfg = _cfg(ContextOverflowPolicy.REFUSE, budget=100)
    transcript = [
        _FakeEntry(
            content="small visible answer",
            token_count=1,
            tool_calls=[
                {
                    "type": "tool_use",
                    "tool_use_id": "write-stale",
                    "name": "write_file",
                    "input": {"path": "index.html", "content": "x" * 5000},
                }
            ],
        )
    ]

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="next",
        transcript=transcript,
        session_key="s-tool-payload",
    )

    assert outcome.over_budget is True
    assert outcome.refusal is not None
    assert outcome.estimated_tokens > cfg.context_budget_tokens


@pytest.mark.asyncio
async def test_gateway_context_overflow_counts_reasoning_content() -> None:
    cfg = _cfg(ContextOverflowPolicy.REFUSE, budget=100)
    transcript = [
        _FakeEntry(
            content="small visible answer",
            token_count=1,
            reasoning_content="reasoning " + ("r" * 5000),
        )
    ]

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="next",
        transcript=transcript,
        session_key="s-reasoning-payload",
    )

    assert outcome.over_budget is True
    assert outcome.refusal is not None
    assert outcome.estimated_tokens > cfg.context_budget_tokens


@pytest.mark.asyncio
async def test_hard_truncate_drops_oldest_history_until_fits() -> None:
    """HARD_TRUNCATE removes oldest entries one at a time to fit the budget."""

    cfg = _cfg(ContextOverflowPolicy.HARD_TRUNCATE, budget=10)
    transcript = _history(5, 40)  # 5 * 40 chars ≈ 50 tokens per estimate_tokens
    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=transcript,
        session_key="s-trunc",
    )
    assert outcome.over_budget is True
    assert outcome.truncated_entries > 0
    # Some entries were dropped; remaining history is shorter than input.
    assert len(outcome.trimmed_history) == len(transcript) - outcome.truncated_entries


@pytest.mark.asyncio
async def test_auto_summarize_invokes_compaction_and_retries_once() -> None:
    """AUTO_SUMMARIZE retries only after compacted payload is inside budget."""

    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10)
    sm = _FakeSessionManager(_history(6, 40))
    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="s-auto",
        session_manager=sm,
    )
    assert outcome.over_budget is True
    assert outcome.summarized is True
    assert outcome.retried is True
    assert len(sm.compact_calls) == 1
    assert sm.compact_calls[0][0] == "s-auto"
    assert outcome.tokens_after is not None
    assert outcome.remaining_budget_tokens is not None
    assert outcome.tokens_after <= outcome.budget_tokens


@pytest.mark.asyncio
async def test_auto_summarize_preserves_root_and_splits_auxiliary_executions() -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10, flush_enabled=True)
    sm = _ResultCompactionSessionManager(_history(6, 40))
    flush_service = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                mode="llm",
                integrity_ok=True,
                output_coverage_status="ok",
                invalid_candidate_count=0,
                candidate_missing_ids=[],
                obligation_status="ok",
                obligation_missing_ids=[],
            )
        )
    )
    compaction_correlation = ProviderRequestCorrelation(
        session_id="durable-session-1",
        turn_id="overflow-turn-1",
        execution_id="compaction-execution-1",
        call_kind="auxiliary.compaction",
    )

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="agent:main:s-correlation",
        session_manager=sm,
        flush_service=flush_service,
        provider_request_correlation=compaction_correlation,
        root_operation_id=compaction_correlation.turn_id,
    )
    await asyncio.sleep(0)

    assert outcome.summarized is True
    assert (
        sm.compact_kwargs[0]["provider_request_correlation"]
        is compaction_correlation
    )
    assert sm.compact_kwargs[0]["compaction_id"] == compaction_correlation.turn_id
    flush_correlation = flush_service.execute.await_args.kwargs[
        "provider_request_correlation"
    ]
    assert flush_correlation.session_id == compaction_correlation.session_id
    assert flush_correlation.turn_id == compaction_correlation.turn_id
    assert flush_correlation.execution_id != compaction_correlation.execution_id
    assert flush_correlation.call_kind == "auxiliary.session_flush"


@pytest.mark.asyncio
async def test_auto_summarize_checkpoint_runs_before_compact() -> None:
    sm = _CheckpointingSessionManager(_history(6, 40))

    outcome = await apply_context_overflow_policy(
        config=_cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10),
        message="m",
        transcript=sm._transcript,
        session_key="s-checkpoint",
        session_manager=sm,
    )

    assert outcome.summarized is True
    assert sm.calls[0] == "checkpoint"
    assert "compact" in sm.calls


@pytest.mark.asyncio
async def test_auto_summarize_checkpoint_failure_propagates_without_ephemeral_fallback() -> None:
    sm = _FailingCheckpointSessionManager(_history(6, 40))

    with pytest.raises(RuntimeError, match="checkpoint write failed"):
        await apply_context_overflow_policy(
            config=_cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10),
            message="m",
            transcript=sm._transcript,
            session_key="s-checkpoint-fails",
            session_manager=sm,
        )

    assert sm.calls == ["checkpoint"]
    assert sm.compact_calls == []


@pytest.mark.asyncio
async def test_auto_summarize_emits_started_and_completed_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10)
    sm = _ResultCompactionSessionManager(_history(6, 40))
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        context_overflow,
        "notify_compaction",
        lambda session_key, **payload: events.append((session_key, payload)),
    )

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="s-auto-events",
        session_manager=sm,
    )

    assert outcome.summarized is True
    assert [(key, payload["status"]) for key, payload in events] == [
        ("s-auto-events", "started"),
        ("s-auto-events", "observed"),
        ("s-auto-events", "observed"),
        ("s-auto-events", "completed"),
    ]
    assert all(payload["source"] == "automatic" for _, payload in events)
    assert all(payload["phase"] == "gateway_auto_summarize" for _, payload in events)
    compaction_ids = {payload.get("compaction_id") for _, payload in events}
    assert len(compaction_ids) == 1
    assert None not in compaction_ids
    assert events[0][1]["event"] == "compaction.triggered"
    assert events[1][1]["event"] == "compaction.chunk_summarized"
    assert events[2][1]["event"] == "compaction.summary_verified"
    completed = events[-1][1]
    assert completed["event"] == "compaction.replayed"
    assert completed["event_chain"] == [
        "compaction.triggered",
        "compaction.chunk_summarized",
        "compaction.summary_verified",
        "compaction.persisted",
        "compaction.replayed",
    ]
    assert completed["coverage_status"] == "unknown"
    assert completed["chunk_count"] == 2
    assert completed["summary_source"] == "llm"
    assert completed["removed_count"] == 5
    assert completed["kept_count"] == 1
    assert completed["tokens_after"] < completed["tokens_before"]
    assert completed["applied"] is True
    assert completed["durability"] == "durable"
    assert completed["user_visible"] is True


@pytest.mark.asyncio
async def test_auto_summarize_reports_durable_effect_when_request_still_over_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10)
    sm = _InsufficientResultCompactionSessionManager(_history(6, 40))
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        context_overflow,
        "notify_compaction",
        lambda session_key, **payload: events.append((session_key, payload)),
    )

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="s-auto-durable-refused",
        session_manager=sm,
    )

    assert outcome.summarized is False
    assert outcome.lifecycle is not None
    assert outcome.lifecycle.compacted is True
    assert outcome.lifecycle.refused is True
    failed = events[-1][1]
    assert failed["status"] == "failed"
    assert failed["request_status"] == "refused"
    assert failed["reason"] == "compaction_insufficient"
    assert failed["applied"] is True
    assert failed["durability"] == "durable"
    assert failed["removed_count"] == 5


@pytest.mark.asyncio
async def test_auto_summarize_uses_ephemeral_trim_on_compaction_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10)
    sm = _FailingCompactionSessionManager(_history(6, 40))
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        context_overflow,
        "notify_compaction",
        lambda session_key, **payload: events.append((session_key, payload)),
    )

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="s-auto-failed",
        session_manager=sm,
    )

    assert outcome.summarized is False
    assert outcome.reason == "emergency_ephemeral"
    assert outcome.refusal is None
    assert outcome.retried is True
    assert [payload["status"] for _, payload in events] == ["started", "emergency_ephemeral"]
    assert events[-1][1]["reason"] == "emergency_ephemeral"
    assert events[-1][1]["applied"] is True
    assert events[-1][1]["durability"] == "request_scoped"
    assert events[-1][1]["user_visible"] is True
    assert "compact boom" in events[-1][1]["message"]


@pytest.mark.asyncio
async def test_auto_summarize_uses_ephemeral_trim_when_marker_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10)
    sm = _FakeSessionManager(_history(6, 40))
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        context_overflow,
        "notify_compaction",
        lambda session_key, **payload: events.append((session_key, payload)),
    )

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="s-marker-failed",
        session_manager=sm,
        compaction_marker=_FailingTurnCompactionMarker(),
    )

    assert outcome.summarized is False
    assert outcome.reason == "emergency_ephemeral"
    assert outcome.refusal is None
    assert outcome.retried is True
    assert outcome.flush_receipt is None
    assert sm.compact_calls == []
    assert [payload["status"] for _, payload in events] == ["emergency_ephemeral"]
    assert events[-1][1]["reason"] == "emergency_ephemeral"
    assert events[-1][1]["durability"] == "request_scoped"
    assert events[-1][1]["flush_receipt_status"] == "not_required"
    assert "marker unavailable" in events[-1][1]["message"]


@pytest.mark.asyncio
async def test_auto_summarize_refuses_when_compaction_still_exceeds_budget() -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10)
    sm = _InsufficientCompactionSessionManager(_history(6, 40))

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="s-insufficient",
        session_manager=sm,
    )

    assert outcome.over_budget is True
    assert outcome.summarized is False
    assert outcome.retried is False
    assert outcome.reason == "compaction_insufficient"
    assert outcome.refusal is not None
    assert outcome.refusal["error"]["reason"] == "compaction_insufficient"
    assert len(sm.compact_calls) == 1


@pytest.mark.asyncio
async def test_session_payload_estimate_counts_rendered_structured_context_state() -> None:
    structured = StructuredCompactionSummary(
        user_goal="Finish the migration",
        current_status="Current status has enough detail to exceed the plain summary.",
        next_action="Run focused regression tests",
        files_and_artifacts=[
            {
                "path": "src/opensquilla/session/context_view.py",
                "status": "changed",
                "why": "provider-visible compaction context is rendered here",
            }
        ],
        critical_carry_forward=[
            "Do not rely on summary_text when structured context state is valid."
        ],
    )
    summary = SessionSummary(
        session_id="sid",
        session_key="s-structured",
        summary_text="short summary",
        covered_through_id=7,
    )
    state = SessionContextState(
        session_id="sid",
        session_key="s-structured",
        provider="portable",
        state_kind="structured_summary_v1",
        payload=structured.model_dump(mode="json"),
        covered_through_id=7,
        portable=True,
        valid=True,
    )
    sm = _StructuredContextSessionManager([], summaries=[summary], context_states=[state])

    total = await context_overflow._estimate_session_payload_tokens(
        "",
        [],
        session_manager=sm,
        session_key="s-structured",
    )

    assert total == context_overflow._estimate_payload_tokens("", []) + estimate_tokens(
        render_structured_summary(structured)
    )


@pytest.mark.asyncio
async def test_auto_summarize_uses_fallback_summary_when_context_cannot_be_verified() -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10)
    sm = _SummaryReadFailureSessionManager(_history(6, 40))

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="s-summary-fail",
        session_manager=sm,
    )

    assert outcome.over_budget is True
    assert outcome.summarized is True
    assert outcome.retried is True
    assert outcome.reason is None
    assert outcome.refusal is None
    assert len(sm.compact_calls) == 1


@pytest.mark.asyncio
async def test_auto_summarize_compacts_while_protect_flush_runs_in_background() -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10, flush_enabled=True)
    sm = _FakeSessionManager(_history(6, 40))
    flush_service = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                mode="llm",
                integrity_ok=False,
                output_coverage_status="ok",
                missing_candidate_count=0,
                invalid_candidate_count=0,
                obligation_status="ok",
            )
        )
    )

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="agent:main:s-flush",
        session_manager=sm,
        flush_service=flush_service,
    )

    assert outcome.over_budget is True
    assert outcome.summarized is True
    assert outcome.retried is True
    assert outcome.reason is None
    assert outcome.refusal is None
    assert outcome.flush_receipt is None
    assert outcome.lifecycle is not None
    assert outcome.lifecycle.flush_receipt is outcome.flush_receipt
    assert outcome.lifecycle.refused is False
    assert sm.compact_calls == [("agent:main:s-flush", 10, None)]
    await asyncio.sleep(0)
    flush_service.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_summarize_flush_enabled_without_trigger_skips_flush_service() -> None:
    cfg = _cfg(
        ContextOverflowPolicy.AUTO_SUMMARIZE,
        budget=10,
        flush_enabled=True,
        flush_pre_compaction=False,
    )
    sm = _FakeSessionManager(_history(6, 40))
    flush_service = SimpleNamespace(execute=AsyncMock())

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="agent:main:s-flush-disabled-trigger",
        session_manager=sm,
        flush_service=flush_service,
    )

    assert outcome.over_budget is True
    assert outcome.summarized is True
    assert outcome.retried is True
    assert outcome.flush_receipt is None
    flush_service.execute.assert_not_called()
    assert sm.compact_calls == [("agent:main:s-flush-disabled-trigger", 10, None)]


@pytest.mark.asyncio
async def test_auto_summarize_compacts_when_distill_fails_after_checkpoint() -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10, flush_enabled=True)
    sm = _CheckpointingSessionManager(_history(6, 40))
    flush_service = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("bad json"))
    )

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="agent:main:s-distill-fails",
        session_manager=sm,
        flush_service=flush_service,
    )

    assert outcome.over_budget is True
    assert outcome.summarized is True
    assert outcome.retried is True
    assert outcome.reason is None
    assert outcome.refusal is None
    assert outcome.flush_receipt is None
    assert sm.calls == ["checkpoint", "compact"]
    assert sm.compact_calls == [("agent:main:s-distill-fails", 10, None)]
    await asyncio.sleep(0)
    assert flush_service.execute.await_args.kwargs["message_window"] == 0


@pytest.mark.asyncio
async def test_auto_summarize_strict_semantic_failure_after_checkpoint_refuses() -> None:
    cfg = _cfg(
        ContextOverflowPolicy.AUTO_SUMMARIZE,
        budget=10,
        flush_enabled=True,
        flush_compaction_requires_safe_receipt=True,
    )
    sm = _CheckpointingSessionManager(_history(6, 40))
    flush_service = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                mode="error",
                result_status="archive_failed",
                flushed_paths=[],
                content_hash="h1",
                indexed_chunk_count=0,
                integrity_status="unverified",
                output_coverage_status="unverified",
                invalid_candidate_count=0,
                candidate_missing_ids=[],
                obligation_status="unverified",
                obligation_missing_ids=[],
            )
        )
    )

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="agent:main:s-distill-fails-after-checkpoint",
        session_manager=sm,
        flush_service=flush_service,
    )

    assert outcome.over_budget is True
    assert outcome.summarized is False
    assert outcome.retried is False
    assert outcome.reason == "compaction_flush_failed"
    assert outcome.refusal is not None
    assert sm.calls == ["checkpoint"]
    assert sm.compact_calls == []


@pytest.mark.asyncio
async def test_auto_summarize_strict_invalid_checkpoint_receipt_refuses_compaction() -> None:
    cfg = _cfg(
        ContextOverflowPolicy.AUTO_SUMMARIZE,
        budget=10,
        flush_enabled=True,
        flush_compaction_requires_safe_receipt=True,
    )
    sm = _InvalidCheckpointSessionManager(_history(6, 40))
    flush_service = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                mode="error",
                result_status="archive_failed",
                flushed_paths=[],
                content_hash="h1",
                indexed_chunk_count=0,
                integrity_status="unverified",
                output_coverage_status="unverified",
                invalid_candidate_count=0,
                candidate_missing_ids=[],
                obligation_status="unverified",
                obligation_missing_ids=[],
            )
        )
    )

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="agent:main:s-invalid-checkpoint-receipt",
        session_manager=sm,
        flush_service=flush_service,
    )

    assert outcome.over_budget is True
    assert outcome.summarized is False
    assert outcome.retried is False
    assert outcome.reason == "compaction_flush_failed"
    assert outcome.refusal is not None
    assert outcome.refusal["error"]["memory_safety_status"] == "unsafe"
    assert outcome.refusal["error"]["semantic_memory_status"] == "failed"
    assert sm.calls == ["checkpoint"]
    assert sm.compact_calls == []

@pytest.mark.asyncio
async def test_auto_summarize_strict_flush_receipt_refuses_before_compaction() -> None:
    cfg = _cfg(
        ContextOverflowPolicy.AUTO_SUMMARIZE,
        budget=10,
        flush_enabled=True,
        flush_compaction_requires_safe_receipt=True,
    )
    sm = _FakeSessionManager(_history(6, 40))
    flush_service = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                mode="llm",
                integrity_status="missing_chunks",
                indexed_chunk_count=1,
                output_coverage_status="ok",
                invalid_candidate_count=0,
                candidate_missing_ids=[],
                obligation_status="ok",
                obligation_missing_ids=[],
            )
        )
    )

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="agent:main:s-strict-flush",
        session_manager=sm,
        flush_service=flush_service,
    )

    assert outcome.over_budget is True
    assert outcome.summarized is False
    assert outcome.retried is False
    assert outcome.reason == "compaction_flush_failed"
    assert outcome.refusal is not None
    assert outcome.refusal["error"]["reason"] == "compaction_flush_failed"
    assert outcome.refusal["error"]["memory_safety_status"] == "unsafe"
    assert outcome.refusal["error"]["semantic_memory_status"] == "degraded"
    assert outcome.flush_receipt is not None
    assert outcome.lifecycle is not None
    assert outcome.lifecycle.refused is True
    assert outcome.lifecycle.reason == "compaction_flush_failed"
    assert sm.compact_calls == []


@pytest.mark.asyncio
async def test_auto_summarize_protect_flush_receipt_degrades_without_refusal() -> None:
    cfg = _cfg(
        ContextOverflowPolicy.AUTO_SUMMARIZE,
        budget=10,
        flush_enabled=True,
        flush_compaction_safety_mode="protect",
    )
    sm = _ResultCompactionSessionManager(_history(6, 40))
    flush_service = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                mode="llm",
                integrity_status="missing_chunks",
                indexed_chunk_count=1,
                output_coverage_status="ok",
                invalid_candidate_count=0,
                candidate_missing_ids=[],
                obligation_status="ok",
                obligation_missing_ids=[],
            )
        )
    )

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="agent:main:s-protect-flush",
        session_manager=sm,
        flush_service=flush_service,
    )

    assert outcome.over_budget is True
    assert outcome.summarized is True
    assert outcome.retried is True
    assert outcome.reason is None
    assert outcome.refusal is None
    assert sm.compact_calls == [("agent:main:s-protect-flush", 10, None)]
    assert sm.compact_kwargs[0]["flush_receipt_status"] == "degraded_forensic"
    assert sm.compact_kwargs[0]["trigger_reason"] == "gateway_auto_summarize"
    await asyncio.sleep(0)
    flush_service.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_summarize_compaction_failure_uses_ephemeral_trim() -> None:
    class _FailingSessionManager(_FakeSessionManager):
        async def compact(self, session_key: str, budget: int, config=None) -> str:
            self.compact_calls.append((session_key, budget, config))
            raise RuntimeError("preimage unavailable")

    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10, flush_enabled=False)
    sm = _FailingSessionManager(_history(6, 40))

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="agent:main:s-emergency",
        session_manager=sm,
    )

    assert outcome.over_budget is True
    assert outcome.summarized is False
    assert outcome.retried is True
    assert outcome.refusal is None
    assert outcome.reason == "emergency_ephemeral"
    assert outcome.truncated_entries > 0
    assert len(outcome.trimmed_history) < len(sm._transcript)


@pytest.mark.asyncio
async def test_auto_summarize_compacts_when_flush_service_is_missing() -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10, flush_enabled=True)
    sm = _FakeSessionManager(_history(6, 40))

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="agent:main:s-missing-flush",
        session_manager=sm,
    )

    assert outcome.over_budget is True
    assert outcome.summarized is True
    assert outcome.retried is True
    assert outcome.reason is None
    assert outcome.refusal is None
    assert sm.compact_calls == [("agent:main:s-missing-flush", 10, None)]


@pytest.mark.asyncio
async def test_auto_summarize_compacts_while_slow_flush_runs_in_background() -> None:
    cfg = _cfg(
        ContextOverflowPolicy.AUTO_SUMMARIZE,
        budget=10,
        flush_enabled=True,
        flush_timeout_seconds=0.001,
        flush_background_timeout_seconds=42.0,
    )
    sm = _FakeSessionManager(_history(6, 40))
    flush_started = asyncio.Event()
    flush_release = asyncio.Event()

    async def _slow_flush(*args: Any, **kwargs: Any) -> Any:
        flush_started.set()
        await flush_release.wait()
        return SimpleNamespace(
            mode="llm",
            integrity_ok=True,
            output_coverage_status="ok",
            missing_candidate_count=0,
            invalid_candidate_count=0,
            obligation_status="ok",
            timeout_seconds=kwargs.get("timeout"),
        )

    flush_service = SimpleNamespace(execute=AsyncMock(side_effect=_slow_flush))

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="agent:main:s-slow-flush",
        session_manager=sm,
        flush_service=flush_service,
    )

    await asyncio.wait_for(flush_started.wait(), timeout=1.0)
    assert outcome.over_budget is True
    assert outcome.summarized is True
    assert outcome.retried is True
    assert outcome.reason is None
    assert outcome.refusal is None
    assert sm.compact_calls == [("agent:main:s-slow-flush", 10, None)]
    assert flush_service.execute.await_args.kwargs["timeout"] == 42.0

    flush_release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_auto_summarize_does_not_compact_twice_in_same_turn() -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10)
    sm = _InsufficientCompactionSessionManager(_history(6, 40))
    marker = _TurnCompactionMarker({"s-once"})

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="s-once",
        session_manager=sm,
        compaction_marker=marker,
    )

    assert outcome.over_budget is True
    assert outcome.reason == "compaction_insufficient"
    assert sm.compact_calls == []


@pytest.mark.asyncio
async def test_failed_auto_summarize_does_not_mark_turn_compacted() -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10)
    sm = _InsufficientCompactionSessionManager(_history(6, 40))
    marker = _TurnCompactionMarker()

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="s-failed",
        session_manager=sm,
        compaction_marker=marker,
    )

    assert outcome.reason == "compaction_insufficient"
    assert outcome.compacted_this_turn is False
    assert marker.mark_calls == []
    assert "s-failed" not in marker.compacted


@pytest.mark.asyncio
async def test_auto_summarize_forwards_compaction_config() -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10)
    sm = _FakeSessionManager(_history(6, 40))
    compaction_config = CompactionConfig(api_key="key", model="model")

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="s-auto",
        session_manager=sm,
        compaction_config=compaction_config,
    )

    assert outcome.summarized is True
    assert sm.compact_calls == [("s-auto", 10, compaction_config)]


@pytest.mark.asyncio
async def test_auto_summarize_keeps_legacy_compact_manager_compatible() -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10)
    sm = _LegacyCompactSessionManager(_history(6, 40))
    compaction_config = CompactionConfig(api_key="key", model="model")

    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=sm._transcript,
        session_key="s-auto",
        session_manager=sm,
        compaction_config=compaction_config,
    )

    assert outcome.summarized is True
    assert sm.compact_calls == [("s-auto", 10, None)]


@pytest.mark.asyncio
async def test_chat_send_accepts_turn_without_synchronous_context_overflow_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10)
    sm = SimpleNamespace(
        get_or_create=AsyncMock(return_value=SimpleNamespace(session_key="s-auto")),
    )
    ctx = SimpleNamespace(
        config=cfg,
        session_manager=sm,
        principal=SimpleNamespace(role="owner"),
    )
    accepted: dict[str, Any] = {}

    async def _unexpected_gate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("chat.send must not synchronously refuse overflow")

    async def _fake_sessions_send(
        params: dict[str, Any],
        _ctx: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        accepted.update(params)
        return {"status": "accepted", "key": params["key"], "task_id": "task-long-context"}

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_chat._enforce_context_overflow",
        _unexpected_gate,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._handle_sessions_send",
        _fake_sessions_send,
    )

    result = await _handle_chat_send({"message": "m", "sessionKey": "s-auto"}, ctx)

    assert result == {
        "ok": True,
        "sessionKey": "s-auto",
        "status": "accepted",
        "key": "s-auto",
        "task_id": "task-long-context",
    }
    assert accepted["message"] == "m"
    assert accepted["key"] == "s-auto"


def test_chat_send_creates_webchat_session_with_agent_from_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10)
    sm = SimpleNamespace(
        get_or_create=AsyncMock(
            return_value=SimpleNamespace(
                session_key="agent:kid-project:webchat:abc",
                agent_id="kid-project",
            )
        ),
    )
    ctx = SimpleNamespace(
        config=cfg,
        session_manager=sm,
        principal=SimpleNamespace(role="owner"),
    )

    async def _fake_sessions_send(
        params: dict[str, Any],
        _ctx: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return {"status": "accepted", "key": params["key"], "task_id": "task-1"}

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._handle_sessions_send",
        _fake_sessions_send,
    )

    async def _run() -> dict[str, Any]:
        return await _handle_chat_send(
            {"message": "m", "sessionKey": "agent:kid-project:webchat:abc"},
            ctx,
        )

    result = asyncio.run(_run())

    assert result["ok"] is True
    sm.get_or_create.assert_awaited_once_with(
        session_key="agent:kid-project:webchat:abc",
        agent_id="kid-project",
        display_name="WebChat",
    )


@pytest.mark.asyncio
async def test_rpc_chat_auto_summarize_builds_provider_compaction_config() -> None:
    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10)
    sm = _FakeSessionManager(_history(6, 40))
    sm._storage = SimpleNamespace(
        get_session=AsyncMock(
            return_value=SimpleNamespace(model="session/model", model_override="routed/model")
        )
    )
    selector = _FakeProviderSelector()
    ctx = SimpleNamespace(config=cfg, session_manager=sm, provider_selector=selector)

    refusal = await _enforce_context_overflow(ctx, "s-auto", "m")

    assert refusal is None
    config = sm.compact_calls[0][2]
    assert isinstance(config, CompactionConfig)
    assert config.api_key == "overflow-provider-key"
    assert config.model == "routed/model"
    assert config.base_url == "https://openrouter.ai/api/v1"
    assert selector.override_calls == []
    assert selector.clone_instance.override_calls == ["routed/model"]


@pytest.mark.asyncio
async def test_auto_summarize_without_session_manager_uses_proxy() -> None:
    """Without a session manager, AUTO degrades to drop-oldest proxy."""

    cfg = _cfg(ContextOverflowPolicy.AUTO_SUMMARIZE, budget=10)
    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="m",
        transcript=_history(6, 40),
        session_key="s-proxy",
        session_manager=None,
    )
    assert outcome.over_budget is True
    assert outcome.retried is True
    assert outcome.summarized is False
    assert outcome.truncated_entries > 0


@pytest.mark.asyncio
async def test_outcome_carries_diagnostic_counters() -> None:
    """The returned OverflowOutcome exposes estimated + budget for observability."""

    cfg = _cfg(ContextOverflowPolicy.REFUSE, budget=3)
    outcome = await apply_context_overflow_policy(
        config=cfg,
        message="hello",
        transcript=_history(2, 40),
        session_key="s-x",
    )
    assert isinstance(outcome, OverflowOutcome)
    assert outcome.estimated_tokens > outcome.budget_tokens
    assert outcome.budget_tokens == 3
