from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
)
from opensquilla.provider.failures import ProviderFailureKind
from opensquilla.provider.protocol import ProviderConnectionConfig, ProviderMetadata
from opensquilla.provider.selector import ProviderConfig, build_provider_from_config
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    ProviderRequestCorrelation,
    ReasoningDeltaEvent,
    TextDeltaEvent,
)
from opensquilla.session.compaction import (
    CompactionRequest,
    arm_compaction_deadline,
    build_compaction_config_from_provider,
    call_compaction_provider,
    compact_context,
)
from opensquilla.session.compaction_deployment import (
    CompactionExecutionPlan,
    CompactionExecutionTarget,
    build_compaction_llm_plan_from_provider_config,
)


class _Stream:
    def __init__(self, events: list[Any]) -> None:
        self._events = iter(events)
        self.closed = False

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        self.closed = True


class _BlockingStream:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    def __aiter__(self) -> _BlockingStream:
        return self

    async def __anext__(self) -> Any:
        self.started.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


class _Provider:
    provider_name = "openai"

    def __init__(self, stream_factory) -> None:
        self._stream_factory = stream_factory
        self.calls: list[tuple[list[Any], list[Any] | None, ChatConfig | None]] = []
        self.streams: list[Any] = []

    def provider_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_name="openai",
            provider_kind="openrouter",
            provider_id="openrouter",
            model="provider/model",
            base_url="https://openrouter.ai/api/v1",
        )

    def provider_connection_config(self) -> ProviderConnectionConfig:
        return ProviderConnectionConfig(
            provider_kind="openrouter",
            model="provider/model",
            api_key="super-secret",
            base_url="https://openrouter.ai/api/v1",
        )

    def chat(self, messages, tools=None, config=None):
        self.calls.append((messages, tools, config))
        stream = self._stream_factory()
        self.streams.append(stream)
        return stream

    async def list_models(self) -> list[Any]:
        return []


@dataclass
class _Sink:
    starts: list[Any] = field(default_factory=list)
    finalized: list[tuple[Any, Any]] = field(default_factory=list)
    unknown: list[tuple[Any, str]] = field(default_factory=list)

    async def start(self, call: Any) -> None:
        self.starts.append(call)

    async def finalize(self, call: Any, result: Any) -> None:
        self.finalized.append((call, result))

    async def mark_unknown(self, call: Any, reason: str) -> None:
        self.unknown.append((call, reason))


class _CancellationResistantSink(_Sink):
    """A usage sink that deliberately keeps waiting after task cancellation."""

    def __init__(self) -> None:
        super().__init__()
        self.unknown_started = asyncio.Event()
        self.unknown_cancelled = asyncio.Event()
        self.unknown_finished = asyncio.Event()
        self.release_unknown = asyncio.Event()
        self.before_unknown: Any = None
        self.raw_closed_when_unknown_started: bool | None = None

    async def mark_unknown(self, call: Any, reason: str) -> None:
        if callable(self.before_unknown):
            self.raw_closed_when_unknown_started = bool(self.before_unknown())
        self.unknown.append((call, reason))
        self.unknown_started.set()
        try:
            await self.release_unknown.wait()
        except asyncio.CancelledError:
            self.unknown_cancelled.set()
            await self.release_unknown.wait()
        finally:
            self.unknown_finished.set()


def _usage_scope(sink: _Sink) -> UsageAccountingScope:
    return UsageAccountingScope(
        sink=sink,
        context=UsageExecutionContext(
            execution_id="compaction-execution",
            agent_run_id="compaction-run",
            turn_id="turn-1",
            session_id="session-1",
            agent_id="main",
            run_kind="compaction",
        ),
    )


def _successful_stream() -> _Stream:
    return _Stream(
        [
            TextDeltaEvent(text="portable "),
            TextDeltaEvent(text="summary"),
            DoneEvent(
                input_tokens=12,
                output_tokens=2,
                model="provider/model",
                provider="openrouter",
            ),
        ]
    )


def _entries(count: int) -> list[dict[str, Any]]:
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"message {index}",
            "token_count": 100,
        }
        for index in range(count)
    ]


def test_build_provider_from_config_preserves_every_field_and_isolates_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ProviderConfig] = []
    sentinel = object()

    def fake_build(config: ProviderConfig) -> object:
        captured.append(config)
        return sentinel

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", fake_build)
    source = ProviderConfig(
        provider="openrouter",
        model="provider/model",
        api_key="super-secret",
        base_url="https://example.invalid/v1",
        org_id="org-1",
        proxy="http://proxy.invalid",
        provider_routing={"order": "latency"},
        replay_provider_state=False,
    )

    assert build_provider_from_config(source) is sentinel
    built = captured[0]
    assert built is not source
    assert built == source
    assert built.provider_routing is not source.provider_routing
    built.provider_routing["order"] = "price"
    assert source.provider_routing == {"order": "latency"}
    assert "super-secret" not in repr(source)
    assert "super-secret" not in repr(built)


def test_full_config_plan_has_candidate_shape_and_no_secret_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider(_successful_stream)
    captured: list[ProviderConfig] = []

    def fake_factory(config: ProviderConfig) -> _Provider:
        captured.append(config)
        return provider

    monkeypatch.setattr(
        "opensquilla.session.compaction_deployment.build_provider_from_config",
        fake_factory,
    )
    plan = build_compaction_llm_plan_from_provider_config(
        ProviderConfig(
            provider="openrouter",
            model="consumer/model",
            api_key="super-secret",
            provider_routing={"order": "latency"},
            replay_provider_state=True,
        ),
        model_override="summary/model",
        context_window_tokens=64_000,
        max_output_tokens=768,
        provider_request_max_chars=120_000,
        source="router_base",
    )

    assert isinstance(plan, CompactionExecutionPlan)
    assert isinstance(plan.primary, CompactionExecutionTarget)
    assert plan.candidates == (plan.primary,)
    assert plan.deployment is plan.primary
    assert plan.primary.model == "summary/model"
    assert plan.primary.context_window_tokens == 64_000
    assert plan.primary.max_output_tokens == 768
    assert plan.primary.provider_request_max_chars == 120_000
    assert plan.primary.portable is True
    assert plan.primary.source == "router_base"
    assert len(plan.primary.deployment_fingerprint) == 24
    assert plan.max_calls == 2
    assert captured[0].replay_provider_state is False
    assert captured[0].provider_routing == {"order": "latency"}
    assert "super-secret" not in repr(plan)
    assert repr(provider) not in repr(plan)

    runtime_config = build_compaction_config_from_provider(
        _Provider(_successful_stream),
        compaction_plan=plan,
    )
    assert runtime_config.llm_plan is plan
    assert runtime_config.model == "summary/model"
    assert runtime_config.provider == "openrouter"
    assert runtime_config.api_key == ""


def test_builder_uses_provider_plan_only_when_bound_model_matches() -> None:
    provider = _Provider(_successful_stream)

    native = build_compaction_config_from_provider(provider)
    overridden = build_compaction_config_from_provider(
        provider,
        compaction_config=type(
            "CompactionSettings",
            (),
            {"enabled": True, "model": "different/model"},
        )(),
    )

    assert native.llm_plan is not None
    assert native.model == "provider/model"
    assert overridden.llm_plan is None
    assert overridden.model == "different/model"
    assert overridden.api_key == "super-secret"
    assert "super-secret" not in repr(native)
    assert "super-secret" not in repr(overridden)


@pytest.mark.asyncio
async def test_provider_compaction_disables_tools_and_thinking_and_accounts_usage() -> None:
    provider = _Provider(_successful_stream)
    plan = CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=provider,
                provider_id="openrouter",
                model="provider/model",
                max_output_tokens=768,
                provider_request_max_chars=120_000,
            ),
        )
    )
    correlation = ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="compaction-execution",
        call_kind="auxiliary.compaction",
    )
    sink = _Sink()

    with bind_usage_accounting_scope(_usage_scope(sink)):
        result = await call_compaction_provider(
            "old conversation",
            "Preserve exact IDs.",
            plan,
            timeout=5.0,
            custom_instructions="Focus on current work.",
            provider_request_correlation=correlation,
        )

    assert result == "portable summary"
    messages, tools, config = provider.calls[0]
    assert tools is None
    assert config is not None
    assert config.max_tokens == 768
    assert config.temperature == 0
    assert config.thinking is False
    assert config.thinking_budget_explicit is False
    assert config.tool_choice is None
    assert config.physical_attempt_limit == 1
    assert config.provider_request_max_chars == 120_000
    assert config.provider_request_correlation is correlation
    assert "Preserve exact IDs." in str(config.system)
    assert "Focus on current work." not in str(config.system)
    assert "Focus on current work." in str(messages[0].content)
    assert provider.streams[0].closed is True
    assert len(sink.starts) == 1
    assert sink.starts[0].provider == "openrouter"
    assert sink.starts[0].model == "provider/model"
    assert len(sink.finalized) == 1
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_provider_error_returns_none_and_closes_stream() -> None:
    provider = _Provider(
        lambda: _Stream([ErrorEvent(message="upstream rejected request", code="bad_request")])
    )
    plan = CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=provider,
                provider_id="openrouter",
                model="provider/model",
            ),
        )
    )

    result = await call_compaction_provider("old", "", plan)

    assert result is None
    assert provider.streams[0].closed is True


@pytest.mark.asyncio
async def test_pooled_provider_auth_failure_is_reported_for_rotation() -> None:
    provider = _Provider(
        lambda: _Stream([ErrorEvent(message="invalid API key", code="401")])
    )
    reported: list[tuple[str, str, ProviderFailureKind]] = []

    def report_failure(
        provider_id: str,
        session_key: str,
        failure_kind: ProviderFailureKind,
    ) -> None:
        reported.append((provider_id, session_key, failure_kind))

    plan = CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=provider,
                provider_id="openai",
                model="provider/model",
                credential_pool_provider="openai",
                credential_pool_session_key="session-pinned",
                credential_pool_failure_reporter=report_failure,
            ),
        )
    )

    result = await call_compaction_provider("old", "", plan)

    assert result is None
    assert reported == [
        ("openai", "session-pinned", ProviderFailureKind.AUTH_INVALID)
    ]
    assert provider.streams[0].closed is True


@pytest.mark.asyncio
async def test_partial_summary_without_done_event_is_rejected() -> None:
    provider = _Provider(lambda: _Stream([TextDeltaEvent(text="partial summary")]))
    plan = CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=provider,
                provider_id="openrouter",
                model="provider/model",
            ),
        )
    )

    result = await call_compaction_provider("old", "", plan)

    assert result is None
    assert provider.streams[0].closed is True


@pytest.mark.asyncio
async def test_provider_output_is_rejected_when_stream_exceeds_local_token_cap() -> None:
    provider = _Provider(
        lambda: _Stream(
            [
                TextDeltaEvent(text="unbounded output " * 64),
                DoneEvent(output_tokens=1),
            ]
        )
    )
    plan = CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=provider,
                provider_id="openai_codex",
                model="gpt-5-codex",
                max_output_tokens=8,
            ),
        )
    )

    result = await call_compaction_provider("old", "", plan)

    assert result is None
    assert provider.calls[0][2] is not None
    assert provider.calls[0][2].max_tokens == 8
    assert provider.streams[0].closed is True


@pytest.mark.asyncio
async def test_provider_output_within_local_token_cap_is_accepted() -> None:
    provider = _Provider(
        lambda: _Stream(
            [
                ReasoningDeltaEvent(text="brief"),
                TextDeltaEvent(text="portable summary"),
                DoneEvent(output_tokens=3, reasoning_content="brief"),
            ]
        )
    )
    plan = CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=provider,
                provider_id="openai_codex",
                model="gpt-5-codex",
                max_output_tokens=16,
            ),
        )
    )

    result = await call_compaction_provider("old", "", plan)

    assert result == "portable summary"
    assert provider.streams[0].closed is True


@pytest.mark.asyncio
async def test_visible_output_exactly_at_cap_needs_no_reasoning_reserve() -> None:
    provider = _Provider(
        lambda: _Stream(
            [
                TextDeltaEvent(text="a"),
                DoneEvent(output_tokens=1),
            ]
        )
    )
    plan = CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=provider,
                provider_id="openai_codex",
                model="gpt-5-codex",
                max_output_tokens=1,
            ),
        )
    )

    result = await call_compaction_provider("old", "", plan)

    assert result == "a"
    assert provider.streams[0].closed is True


@pytest.mark.asyncio
async def test_provider_reported_output_usage_must_fit_local_token_cap() -> None:
    provider = _Provider(
        lambda: _Stream(
            [
                TextDeltaEvent(text="short"),
                DoneEvent(output_tokens=17),
            ]
        )
    )
    plan = CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=provider,
                provider_id="openai_codex",
                model="gpt-5-codex",
                max_output_tokens=16,
            ),
        )
    )

    result = await call_compaction_provider("old", "", plan)

    assert result is None
    assert provider.streams[0].closed is True


@pytest.mark.asyncio
async def test_cancellation_closes_provider_stream() -> None:
    blocking_stream = _BlockingStream()
    provider = _Provider(lambda: blocking_stream)
    plan = CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=provider,
                provider_id="openrouter",
                model="provider/model",
            ),
        )
    )

    task = asyncio.create_task(call_compaction_provider("old", "", plan, timeout=30.0))
    await asyncio.wait_for(blocking_stream.started.wait(), timeout=1.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert blocking_stream.closed is True


@pytest.mark.asyncio
async def test_scoped_cancellation_is_not_blocked_by_hanging_usage_terminal() -> None:
    blocking_stream = _BlockingStream()
    provider = _Provider(lambda: blocking_stream)
    plan = CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=provider,
                provider_id="openrouter",
                model="provider/model",
            ),
        )
    )
    sink = _CancellationResistantSink()

    async def run() -> None:
        with bind_usage_accounting_scope(_usage_scope(sink)):
            await call_compaction_provider("old", "", plan, timeout=30.0)

    task = asyncio.create_task(run())
    await asyncio.wait_for(blocking_stream.started.wait(), timeout=1.0)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
        await asyncio.wait_for(sink.unknown_started.wait(), timeout=1.0)
        await asyncio.wait_for(sink.unknown_cancelled.wait(), timeout=1.0)
        assert blocking_stream.closed is True
        assert len(sink.starts) == 1
        assert sink.finalized == []
        assert [(call.event_id, reason) for call, reason in sink.unknown] == [
            (sink.starts[0].event_id, "cancelled")
        ]
    finally:
        sink.release_unknown.set()
        await asyncio.wait_for(sink.unknown_finished.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_scoped_output_cap_cleanup_is_not_blocked_by_hanging_usage_terminal() -> None:
    provider = _Provider(
        lambda: _Stream(
            [
                TextDeltaEvent(text="unbounded output " * 64),
                DoneEvent(output_tokens=1),
            ]
        )
    )
    plan = CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=provider,
                provider_id="openai_codex",
                model="gpt-5-codex",
                max_output_tokens=8,
            ),
        )
    )
    sink = _CancellationResistantSink()
    sink.before_unknown = lambda: provider.streams[0].closed

    async def run() -> str | None:
        with bind_usage_accounting_scope(_usage_scope(sink)):
            return await call_compaction_provider("old", "", plan)

    try:
        result = await asyncio.wait_for(run(), timeout=1.0)
        await asyncio.wait_for(sink.unknown_started.wait(), timeout=1.0)
        await asyncio.wait_for(sink.unknown_cancelled.wait(), timeout=1.0)
        assert result is None
        assert provider.streams[0].closed is True
        assert sink.raw_closed_when_unknown_started is True
        assert len(sink.starts) == 1
        assert sink.finalized == []
        assert [(call.event_id, reason) for call, reason in sink.unknown] == [
            (
                sink.starts[0].event_id,
                "provider_stream_ended_without_usage",
            )
        ]
    finally:
        sink.release_unknown.set()
        await asyncio.wait_for(sink.unknown_finished.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_compaction_uses_provider_protocol_and_caps_physical_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider(_successful_stream)
    config = build_compaction_config_from_provider(provider)
    config.base_chunk_ratio = 0.1
    config.min_chunk_ratio = 0.1
    config.safety_margin = 1.0

    async def raw_call_forbidden(**_kwargs: Any) -> str:
        raise AssertionError("production compaction must not use raw HTTP")

    monkeypatch.setattr(
        "opensquilla.session.compaction.call_compaction_llm",
        raw_call_forbidden,
    )
    result = await compact_context(
        CompactionRequest(
            session_id="provider-native",
            entries=_entries(30),
            context_window_tokens=500,
            config=config,
        )
    )

    assert result.summary_source == "llm"
    assert result.chunks_processed == 1
    assert len(provider.calls) == 1
    assert config.llm_calls_started == 1
    assert all(stream.closed for stream in provider.streams)
    assert result.quality_report["physical_call_count"] == 1
    assert result.quality_report["target_provider"] == "openrouter"
    assert result.quality_report["target_model"] == "provider/model"
    assert result.quality_report["target_source"] == "resolved_provider"
    assert result.quality_report["target_window_source"] in {
        "model_catalog",
        "caller_resolved",
    }
    assert result.quality_report["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_rolling_summary_replaces_previous_checkpoint() -> None:
    provider = _Provider(
        lambda: _Stream(
            [
                TextDeltaEvent(text="replacement checkpoint"),
                DoneEvent(model="provider/model", provider="openrouter"),
            ]
        )
    )
    config = build_compaction_config_from_provider(
        provider,
        context_window_tokens=8_000,
    )
    config.safety_margin = 1.0

    result = await compact_context(
        CompactionRequest(
            session_id="rolling-provider-native",
            entries=_entries(20),
            context_window_tokens=500,
            config=config,
            previous_summary="OLD_UNIQUE_CHECKPOINT",
        )
    )

    assert len(provider.calls) == 1
    sent_content = provider.calls[0][0][0].content
    assert isinstance(sent_content, str)
    assert "OLD_UNIQUE_CHECKPOINT" in sent_content
    assert result.summary == "replacement checkpoint"
    assert "OLD_UNIQUE_CHECKPOINT" not in result.summary
    assert result.summary_payload is not None
    assert result.summary_payload["source_coverage"]["replaces_prior_context"] is True


@pytest.mark.asyncio
async def test_rolling_summary_can_replace_oversized_checkpoint_without_raw_entries() -> None:
    provider = _Provider(
        lambda: _Stream(
            [
                TextDeltaEvent(text="small replacement"),
                DoneEvent(model="provider/model", provider="openrouter"),
            ]
        )
    )
    config = build_compaction_config_from_provider(
        provider,
        context_window_tokens=8_000,
    )
    config.safety_margin = 1.0

    result = await compact_context(
        CompactionRequest(
            session_id="rolling-checkpoint-only",
            entries=[],
            context_window_tokens=500,
            config=config,
            previous_summary="oversized checkpoint " * 2_000,
        )
    )

    assert len(provider.calls) == 1
    assert result.removed_count == 0
    assert result.replaced_previous_summary is True
    assert result.summary == "small replacement"
    assert result.tokens_after < result.tokens_before
    assert result.summary_payload is not None
    assert result.summary_payload["source_coverage"]["replaces_prior_context"] is True


@pytest.mark.asyncio
async def test_fallback_replans_summary_input_for_its_own_smaller_window() -> None:
    primary = _Provider(
        lambda: _Stream(
            [
                ErrorEvent(message="primary unavailable", code="unavailable"),
            ]
        )
    )
    fallback = _Provider(_successful_stream)
    config = build_compaction_config_from_provider(primary)
    config.llm_plan = CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=primary,
                provider_id="openrouter",
                model="large/model",
                context_window_tokens=8_000,
                max_output_tokens=768,
                provider_request_max_chars=24_000,
                source="active",
            ),
            CompactionExecutionTarget(
                provider=fallback,
                provider_id="openrouter",
                model="small/model",
                context_window_tokens=600,
                max_output_tokens=128,
                provider_request_max_chars=1_600,
                source="fallback",
            ),
        ),
        max_calls=2,
    )
    config.safety_margin = 1.0

    result = await compact_context(
        CompactionRequest(
            session_id="fallback-replan",
            entries=_entries(30),
            context_window_tokens=500,
            config=config,
        )
    )

    assert result.summary_source == "llm"
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1
    primary_content = primary.calls[0][0][0].content
    fallback_content = fallback.calls[0][0][0].content
    assert isinstance(primary_content, str)
    assert isinstance(fallback_content, str)
    assert len(fallback_content) < len(primary_content)
    assert primary.calls[0][1] is None
    assert fallback.calls[0][1] is None
    assert primary.calls[0][2] is not None
    assert fallback.calls[0][2] is not None
    assert primary.calls[0][2].max_tokens == 768
    assert fallback.calls[0][2].max_tokens == 128
    assert primary.calls[0][2].candidate_output_mode == "inert_artifact"
    assert fallback.calls[0][2].candidate_output_mode == "inert_artifact"


def test_new_operation_rearms_deadline_and_call_budget() -> None:
    provider = _Provider(_successful_stream)
    config = build_compaction_config_from_provider(provider)

    arm_compaction_deadline(config, operation_id="first")
    config.llm_calls_started = 2
    arm_compaction_deadline(config, operation_id="second")

    assert config.llm_calls_started == 0


def test_execution_plan_refuses_more_than_two_calls() -> None:
    provider = _Provider(_successful_stream)
    target = CompactionExecutionTarget(
        provider=provider,
        provider_id="openrouter",
        model="provider/model",
    )

    with pytest.raises(ValueError, match="between 1 and 2"):
        CompactionExecutionPlan(candidates=(target,), max_calls=3)
