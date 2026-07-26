from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, SubagentSpec
from opensquilla.engine.outcome import outcome_from_error
from opensquilla.engine.pricing import PriceEntry, ResolvedModelPrice
from opensquilla.engine.runtime import TurnRunner, _SelectorFallbackProvider
from opensquilla.engine.types import ErrorEvent
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageAccountingUnavailableError,
    UsageCallResult,
    UsageCallStart,
    UsageExecutionContext,
    account_provider_stream,
    bind_usage_accounting_scope,
    normalize_provider_usage,
    usd_to_nanos,
)
from opensquilla.provider import (
    ChatConfig,
    Message,
    ModelCapabilities,
    ProviderRequestCorrelation,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider.ensemble import EnsembleMemberConfig, EnsembleProvider
from opensquilla.provider.preset_registry import get_preset
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.types import ContentBlockImage, ProviderBillingReceipt
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.skills.meta.orchestrator import make_llm_chat_from_provider
from opensquilla.tools.types import CallerKind, ToolContext
from opensquilla.usage_reasons import (
    normalize_usage_unknown_reason,
    provider_error_usage_reason,
)


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


class _UnavailableSink(_RecordingSink):
    async def start(self, call: UsageCallStart) -> None:
        self.started.append(call)
        raise UsageAccountingUnavailableError("ledger busy")


class _DoneProvider:
    provider_name = "fake"

    def __init__(self, sink: _RecordingSink) -> None:
        self.sink = sink
        self.calls = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        # start() is a fail-closed barrier before provider.chat is invoked.
        assert len(self.sink.started) == self.calls + 1
        self.calls += 1
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="ok")
        yield ProviderDone(
            input_tokens=11,
            output_tokens=3,
            cached_tokens=2,
            billed_cost=0.000000123,
            cost_source="provider_billed",
            model="model-a",
        )


class _CorrelationCapturingProvider(_DoneProvider):
    def __init__(self, sink: _RecordingSink) -> None:
        super().__init__(sink)
        self.configs: list[ChatConfig | None] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.configs.append(config)
        return super().chat(messages, tools=tools, config=config)


class _ErrorProvider:
    provider_name = "fake"
    retry_failed_call_safe = False

    def __init__(self, code: str = "401") -> None:
        self.code = code

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderError(message="denied", code=self.code)


class _BlockingProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        self.entered.set()
        await asyncio.Event().wait()
        if False:  # pragma: no cover - make this an async generator
            yield None


class _SequenceProvider:
    provider_name = "fake"

    def __init__(self, streams: list[list[Any]]) -> None:
        self.streams = streams
        self.calls = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        events = self.streams[self.calls]
        self.calls += 1
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event


class _PhysicalLegProvider(_SequenceProvider):
    def __init__(self, name: str, events: list[Any]) -> None:
        super().__init__([events])
        self.provider_name = name


class _CorrelationCapturingPhysicalLegProvider(_PhysicalLegProvider):
    def __init__(self, name: str, events: list[Any]) -> None:
        super().__init__(name, events)
        self.configs: list[ChatConfig | None] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.configs.append(config)
        return super().chat(messages, tools=tools, config=config)


class _FallbackSelector:
    def __init__(self, fallback: Any) -> None:
        self._fallback = fallback
        self.active_provider_id = "openai"
        self.current_config = SimpleNamespace(model="primary-model")

    def next_fallback_after_failure(self, exc: Exception) -> Any:
        del exc
        self.active_provider_id = "anthropic"
        self.current_config = SimpleNamespace(model="fallback-model")
        return self._fallback


class _SingleProviderSelector:
    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.active_provider_id = "fake"
        self.current_config = SimpleNamespace(model="model-a")

    def clone(self) -> _SingleProviderSelector:
        return _SingleProviderSelector(self.provider)

    def resolve(self) -> Any:
        return self.provider

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)


class _RecordingTracker:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []

    def add(self, session_key: str, **values: Any) -> None:
        self.rows.append((session_key, values))

    def session_checkpoint(self, session_key: str) -> None:
        del session_key
        return None

    def session_delta_snapshot(self, session_key: str, checkpoint: Any) -> None:
        del session_key, checkpoint
        return None

    def session_snapshot(self, session_key: str) -> None:
        del session_key
        return None

    def get(self, session_key: str) -> None:
        del session_key
        return None


def _context() -> UsageExecutionContext:
    return UsageExecutionContext(
        execution_id="turn-1",
        agent_run_id="run-1",
        turn_id="turn-1",
        session_id="session-1",
        session_epoch=7,
        agent_id="main",
        run_kind="webchat",
    )


def _image_rejecting_ensemble(*, fallback_provider: Any | None = None) -> EnsembleProvider:
    return EnsembleProvider(
        profile_name="image-validation",
        proposers=[],
        aggregator=EnsembleMemberConfig(
            provider_config=ProviderConfig(provider="fake", model="never-called")
        ),
        fallback_provider=fallback_provider,
        fallback_provider_name="fake" if fallback_provider is not None else "",
        fallback_model="fallback-model" if fallback_provider is not None else "",
        all_failed_policy="fallback_single" if fallback_provider is not None else "error",
    )


def _image_message() -> Message:
    return Message(
        role="user",
        content=[ContentBlockImage(media_type="image/png", data="aW1hZ2U=")],
    )


class _CapturingTurnLog:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        self.records.append({"kind": kind, "payload": payload})


@pytest.mark.asyncio
async def test_selector_preflight_rejects_ensemble_image_before_usage_or_fallback() -> None:
    sink = _RecordingSink()
    fallback = _PhysicalLegProvider(
        "anthropic",
        [ProviderText(text="must not run"), ProviderDone(model="fallback-model")],
    )
    wrapper = _SelectorFallbackProvider(
        _image_rejecting_ensemble(fallback_provider=fallback),
        _FallbackSelector(fallback),
    )
    scope = UsageAccountingScope(sink=sink, context=_context())

    with bind_usage_accounting_scope(scope):
        events = [event async for event in wrapper.chat([_image_message()])]

    assert [getattr(event, "code", "") for event in events] == [
        "ensemble_multimodal_unsupported"
    ]
    assert fallback.calls == 0
    assert sink.started == []
    assert sink.finalized == []
    assert sink.unknown == []


@pytest.mark.asyncio
@pytest.mark.parametrize("image_location", ["current", "history"])
@pytest.mark.parametrize("wrapped_by_selector", [False, True])
async def test_agent_preflight_rejects_ensemble_image_before_call_accounting(
    image_location: str,
    wrapped_by_selector: bool,
) -> None:
    sink = _RecordingSink()
    tracker = _RecordingTracker()
    fallback = _PhysicalLegProvider(
        "anthropic",
        [ProviderText(text="must not run"), ProviderDone(model="fallback-model")],
    )
    observer_calls: list[dict[str, Any]] = []
    turn_log = _CapturingTurnLog()
    turn_metadata: dict[str, Any] = {}
    ensemble = _image_rejecting_ensemble(fallback_provider=fallback)
    provider: Any = (
        _SelectorFallbackProvider(
            ensemble,
            _FallbackSelector(fallback),
            turn_metadata,
        )
        if wrapped_by_selector
        else ensemble
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_provider_retries=3,
            model_id="ensemble/image-validation",
            model_capabilities=ModelCapabilities(supports_vision=True),
            preserve_historical_images=True,
            provider_call_observer=lambda **kwargs: observer_calls.append(kwargs),
        ),
        usage_tracker=tracker,
        session_key="agent:main:image-validation",
        turn_call_logger=turn_log,  # type: ignore[arg-type]
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )
    if image_location == "history":
        agent.set_history([_image_message()])
        message = "continue"
        extra_messages = None
    else:
        message = ""
        extra_messages = [_image_message()]

    events = [
        event
        async for event in agent.run_turn(
            message,
            extra_messages=extra_messages,
        )
    ]

    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert [error.code for error in errors] == ["ensemble_multimodal_unsupported"]
    assert fallback.calls == 0
    assert sink.started == []
    assert sink.finalized == []
    assert sink.unknown == []
    assert tracker.rows == []
    assert observer_calls == []
    assert "router_fallback_hops" not in turn_metadata
    assert not any(record["kind"] == "llm_request" for record in turn_log.records)
    [decision] = [
        record for record in turn_log.records if record["kind"] == "turn_policy_decision"
    ]
    assert decision["payload"]["code"] == "ensemble_multimodal_unsupported"
    assert "messages" not in decision["payload"]


@pytest.mark.asyncio
async def test_provider_call_is_started_before_chat_and_finalized_once() -> None:
    sink = _RecordingSink()
    provider = _DoneProvider(sink)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1, provider_id="fake", model_id="model-a"),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    async for _ in agent.run_turn("hello"):
        pass

    assert provider.calls == 1
    assert [(call.execution_id, call.call_index) for call in sink.started] == [
        ("turn-1", 1)
    ]
    assert len(sink.finalized) == 1
    assert sink.unknown == []
    call, result = sink.finalized[0]
    assert call.event_id == sink.started[0].event_id
    assert call.session_epoch == 7
    assert result.billed_cost_nanos == 123
    assert result.estimated_cost_nanos == 0
    assert result.cost_source == "provider_billed"


@pytest.mark.asyncio
async def test_provider_request_correlation_reaches_provider_call() -> None:
    sink = _RecordingSink()
    provider = _CorrelationCapturingProvider(sink)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1, provider_id="fake", model_id="model-a"),
        usage_event_sink=sink,
        usage_execution_context=_context(),
        provider_request_correlation=ProviderRequestCorrelation(
            session_id="session-1",
            turn_id="turn-1",
            execution_id="execution-1",
            call_kind="agent.chat",
        ),
    )

    async for _ in agent.run_turn("hello"):
        pass

    assert len(provider.configs) == 1
    config = provider.configs[0]
    assert config is not None
    assert config.provider_request_correlation == ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="execution-1",
        call_kind="agent.chat",
    )


@pytest.mark.asyncio
async def test_ledger_start_failure_is_retryable_and_withholds_provider_request() -> None:
    sink = _UnavailableSink()
    provider = _SequenceProvider([[ProviderDone()]])
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    with pytest.raises(UsageAccountingUnavailableError, match="ledger busy"):
        async for _ in agent.run_turn("hello"):
            pass

    assert provider.calls == 0
    outcome = outcome_from_error(code=UsageAccountingUnavailableError.code)
    assert outcome.kind == "blocked"
    assert outcome.retryable is True


@pytest.mark.asyncio
async def test_provider_error_closes_started_call_as_unknown() -> None:
    sink = _RecordingSink()
    agent = Agent(
        provider=_ErrorProvider(),
        config=AgentConfig(max_iterations=1, max_provider_retries=0),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    async for _ in agent.run_turn("hello"):
        pass

    assert len(sink.started) == 1
    assert sink.finalized == []
    assert [(call.event_id, reason) for call, reason in sink.unknown] == [
        (sink.started[0].event_id, "provider_error:401")
    ]


@pytest.mark.asyncio
async def test_agent_does_not_persist_untrusted_provider_error_code() -> None:
    sink = _RecordingSink()
    agent = Agent(
        provider=_ErrorProvider("https://provider.invalid/error?key=sk-secret\nnext"),
        config=AgentConfig(max_iterations=1, max_provider_retries=0),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    async for _ in agent.run_turn("hello"):
        pass

    assert [reason for _, reason in sink.unknown] == ["provider_error"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("401", "provider_error:401"),
        ("rate_limit_error", "provider_error:rate_limit"),
        ("timeout", "provider_error:timeout"),
        ("vendor_private_code", "provider_error"),
        ("https://provider.invalid/error", "provider_error"),
        ("sk-proj-secret-value", "provider_error"),
        ("401\nrequest-id", "provider_error"),
    ],
)
def test_provider_error_reason_uses_closed_taxonomy(value: str, expected: str) -> None:
    assert provider_error_usage_reason(value) == expected


def test_unknown_reason_normalizer_drops_exception_and_arbitrary_details() -> None:
    assert normalize_usage_unknown_reason("raised:SecretBearingException") == (
        "provider_exception"
    )
    assert normalize_usage_unknown_reason("arbitrary-third-party-value") == "usage_unknown"


@pytest.mark.asyncio
async def test_cancelled_provider_call_is_marked_unknown() -> None:
    sink = _RecordingSink()
    provider = _BlockingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    async def consume() -> None:
        async for _ in agent.run_turn("hello"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(provider.entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sink.started) == 1
    assert sink.finalized == []
    assert len(sink.unknown) == 1
    assert sink.unknown[0][1] == "cancelled"


@pytest.mark.asyncio
async def test_retried_done_calls_get_monotonic_distinct_identities() -> None:
    sink = _RecordingSink()
    provider = _SequenceProvider(
        [
            [ProviderDone(input_tokens=2, output_tokens=0)],
            [ProviderText(text="ok"), ProviderDone(input_tokens=3, output_tokens=1)],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    async for _ in agent.run_turn("hello"):
        pass

    assert provider.calls == 2
    assert [call.call_index for call in sink.started] == [1, 2]
    assert len({call.event_id for call in sink.started}) == 2
    assert [call.event_id for call, _ in sink.finalized] == [
        call.event_id for call in sink.started
    ]
    assert sink.unknown == []


def test_ensemble_breakdown_is_one_envelope_with_items(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "opensquilla.engine.usage_accounting.resolve_model_price",
        lambda model, provider: ResolvedModelPrice(
            entry=PriceEntry(input_per_m=1.0, output_per_m=2.0),
            source="test",
        ),
    )
    event = ProviderDone(
        input_tokens=30,
        output_tokens=5,
        billed_cost=0.5,
        model_usage_breakdown=[
            {
                "provider": "p1",
                "model": "m1",
                "input_tokens": 10,
                "output_tokens": 2,
                "billed_cost": 0.5,
            },
            {
                "provider": "p2",
                "model": "m2",
                "input_tokens": 20,
                "output_tokens": 3,
                "billed_cost": 0.0,
            },
        ],
    )

    result = normalize_provider_usage(
        event,
        default_provider="ensemble",
        default_model="aggregator",
        completed_at_ms=1234,
    )

    assert len(result.items) == 2
    assert result.billed_cost_nanos == usd_to_nanos("0.5")
    assert result.estimated_cost_nanos == usd_to_nanos("0.000026")
    assert result.cost_source == "mixed"
    assert result.input_tokens == sum(item.input_tokens for item in result.items)
    assert result.output_tokens == sum(item.output_tokens for item in result.items)
    assert result.reasoning_tokens == sum(item.reasoning_tokens for item in result.items)
    assert result.cache_read_tokens == sum(
        item.cache_read_tokens for item in result.items
    )
    assert result.cache_write_tokens == sum(
        item.cache_write_tokens for item in result.items
    )
    assert sum(item.billed_cost_nanos for item in result.items) == result.billed_cost_nanos
    assert (
        sum(item.estimated_cost_nanos for item in result.items)
        == result.estimated_cost_nanos
    )


def _tokenrhythm_receipt(*, usd_nanos: int) -> ProviderBillingReceipt:
    """Build an exact receipt at TokenRhythm's fixed 6.975 CNY/USD rate."""

    amount_numerator = usd_nanos * 279
    assert amount_numerator % 40 == 0
    return ProviderBillingReceipt(
        currency="CNY",
        status="confirmed",
        amount_nanos=amount_numerator // 40,
        usd_equivalent_nanos=usd_nanos,
        fx_native_per_usd_nanos=6_975_000_000,
    )


def test_tokenrhythm_single_done_reconciles_native_receipt_and_all_token_buckets() -> None:
    receipt = _tokenrhythm_receipt(usd_nanos=2_000)
    event = ProviderDone(
        input_tokens=101,
        output_tokens=17,
        reasoning_tokens=9,
        cached_tokens=37,
        cache_write_tokens=3,
        billed_cost=0.000002,
        cost_source="provider_billed",
        provider="tokenrhythm",
        model="deepseek-v4-pro",
        billing_receipt=receipt,
    )

    result = normalize_provider_usage(
        event,
        default_provider="tokenrhythm",
        default_model="deepseek-v4-pro",
        completed_at_ms=1234,
    )

    assert len(result.items) == 1
    [item] = result.items
    assert (
        item.input_tokens,
        item.output_tokens,
        item.reasoning_tokens,
        item.cache_read_tokens,
        item.cache_write_tokens,
    ) == (101, 17, 9, 37, 3)
    assert item.billing_receipt == receipt
    assert item.billed_cost_nanos == receipt.usd_equivalent_nanos == 2_000
    assert item.estimated_cost_nanos == 0
    assert item.cost_source == result.cost_source == "provider_billed"
    assert result.billed_cost_nanos == sum(row.billed_cost_nanos for row in result.items)


def test_tokenrhythm_inline_router_c0_c3_reconciles_each_physical_request() -> None:
    preset = get_preset("tokenrhythm")
    assert preset is not None
    tiers = preset.tier_defaults()
    expected_models = {
        "c0": "deepseek-v4-flash",
        "c1": "deepseek-v4-pro",
        "c2": "kimi-k2.7-code",
        "c3": "glm-5.2",
    }

    results: list[UsageCallResult] = []
    for index, (tier, expected_model) in enumerate(expected_models.items(), start=1):
        assert tiers[tier]["provider"] == "tokenrhythm"
        assert tiers[tier]["model"] == expected_model
        usd_nanos = index * 4_000
        receipt = _tokenrhythm_receipt(usd_nanos=usd_nanos)
        event = ProviderDone(
            input_tokens=index * 100,
            output_tokens=index * 10,
            reasoning_tokens=index * 3,
            cached_tokens=index * 20,
            cache_write_tokens=index,
            billed_cost=usd_nanos / 1_000_000_000,
            cost_source="provider_billed",
            provider="tokenrhythm",
            model=expected_model,
            billing_receipt=receipt,
        )
        results.append(
            normalize_provider_usage(
                event,
                default_provider=str(tiers[tier]["provider"]),
                default_model=str(tiers[tier]["model"]),
                completed_at_ms=1234 + index,
            )
        )

    physical_items = [item for result in results for item in result.items]
    assert [item.model for item in physical_items] == list(expected_models.values())
    assert all(item.provider == "tokenrhythm" for item in physical_items)
    assert all(item.cost_source == "provider_billed" for item in physical_items)
    assert all(item.estimated_cost_nanos == 0 for item in physical_items)
    assert sum(item.input_tokens for item in physical_items) == 1_000
    assert sum(item.output_tokens for item in physical_items) == 100
    assert sum(item.reasoning_tokens for item in physical_items) == 30
    assert sum(item.cache_read_tokens for item in physical_items) == 200
    assert sum(item.cache_write_tokens for item in physical_items) == 10
    assert sum(item.billed_cost_nanos for item in physical_items) == 40_000
    assert sum(result.billed_cost_nanos for result in results) == 40_000


def test_incomplete_breakdown_falls_back_to_done_envelope() -> None:
    event = ProviderDone(
        input_tokens=30,
        output_tokens=5,
        cached_tokens=4,
        billed_cost=0.75,
        model="aggregate-model",
        model_usage_breakdown=[
            {
                "provider": "p1",
                "model": "m1",
                "input_tokens": 10,
                "output_tokens": 2,
                "billed_cost": 0.25,
            }
        ],
    )

    result = normalize_provider_usage(
        event,
        default_provider="ensemble",
        default_model="aggregate-model",
        completed_at_ms=1234,
    )

    assert len(result.items) == 1
    assert result.input_tokens == 30
    assert result.output_tokens == 5
    assert result.cache_read_tokens == 4
    assert result.billed_cost_nanos == usd_to_nanos("0.75")
    assert result.items[0].model == "aggregate-model"


def test_partial_ensemble_error_preserves_known_items_and_missing_coverage() -> None:
    event = ProviderError(
        message="aggregator failed",
        code="ensemble_aggregator_error",
        model_usage_breakdown=[
            {
                "provider": "p1",
                "model": "m1",
                "input_tokens": 10,
                "output_tokens": 2,
                "billed_cost": 0.25,
            }
        ],
        usage_missing_count=1,
    )

    result = normalize_provider_usage(
        event,
        default_provider="ensemble",
        default_model="aggregator",
        completed_at_ms=1234,
    )

    assert len(result.items) == 1
    assert result.items[0].model == "m1"
    assert result.billed_cost_nanos == usd_to_nanos("0.25")
    assert result.missing_usage_entries == 1


def test_complete_ensemble_error_preserves_known_items_without_missing_coverage() -> None:
    event = ProviderError(
        message="aggregator was unavailable",
        code="ensemble_aggregator_error",
        model_usage_breakdown=[
            {
                "provider": "p1",
                "model": "m1",
                "input_tokens": 10,
                "output_tokens": 2,
                "billed_cost": 0.25,
            }
        ],
        usage_missing_count=0,
    )

    result = normalize_provider_usage(
        event,
        default_provider="ensemble",
        default_model="aggregator",
        completed_at_ms=1234,
    )

    assert len(result.items) == 1
    assert result.items[0].model == "m1"
    assert result.billed_cost_nanos == usd_to_nanos("0.25")
    assert result.missing_usage_entries == 0


@pytest.mark.asyncio
async def test_partial_ensemble_error_finalizes_outer_call_instead_of_unknown() -> None:
    sink = _RecordingSink()
    scope = UsageAccountingScope(sink=sink, context=_context())

    async def stream() -> AsyncIterator[ProviderError]:
        yield ProviderError(
            message="fallback failed",
            code="ensemble_fallback_incomplete",
            model_usage_breakdown=[
                {
                    "provider": "p1",
                    "model": "m1",
                    "input_tokens": 4,
                    "output_tokens": 2,
                    "billed_cost": 0.1,
                }
            ],
            usage_missing_count=2,
        )

    with bind_usage_accounting_scope(scope):
        events = [
            event
            async for event in account_provider_stream(
                stream,
                provider="ensemble",
                model="aggregator",
            )
        ]

    assert len(events) == 1
    assert len(sink.finalized) == 1
    assert sink.finalized[0][1].missing_usage_entries == 2
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_complete_ensemble_error_finalizes_outer_call_instead_of_unknown() -> None:
    sink = _RecordingSink()
    scope = UsageAccountingScope(sink=sink, context=_context())

    async def stream() -> AsyncIterator[ProviderError]:
        yield ProviderError(
            message="aggregator was unavailable",
            code="ensemble_aggregator_error",
            model_usage_breakdown=[
                {
                    "provider": "p1",
                    "model": "m1",
                    "input_tokens": 4,
                    "output_tokens": 2,
                    "billed_cost": 0.1,
                }
            ],
            usage_missing_count=0,
        )

    with bind_usage_accounting_scope(scope):
        events = [
            event
            async for event in account_provider_stream(
                stream,
                provider="ensemble",
                model="aggregator",
            )
        ]

    assert len(events) == 1
    assert len(sink.finalized) == 1
    assert sink.finalized[0][1].missing_usage_entries == 0
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_agent_finalizes_partial_error_with_a_known_receipt() -> None:
    sink = _RecordingSink()
    provider = _SequenceProvider(
        [
            [
                ProviderError(
                    message="aggregator failed",
                    code="ensemble_aggregator_error",
                    model_usage_breakdown=[
                        {
                            "provider": "p1",
                            "model": "m1",
                            "input_tokens": 4,
                            "output_tokens": 2,
                            "billed_cost": 0.1,
                        }
                    ],
                    usage_missing_count=1,
                )
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1, max_provider_retries=0),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    async for _ in agent.run_turn("hello"):
        pass

    assert len(sink.finalized) == 1
    assert sink.finalized[0][1].missing_usage_entries == 1
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_partial_error_missing_count_without_known_rows_remains_unknown() -> None:
    sink = _RecordingSink()
    scope = UsageAccountingScope(sink=sink, context=_context())

    async def stream() -> AsyncIterator[ProviderError]:
        yield ProviderError(
            message="fallback failed before a receipt",
            code="ensemble_fallback_incomplete",
            model_usage_breakdown=[],
            usage_missing_count=2,
        )

    with bind_usage_accounting_scope(scope):
        events = [
            event
            async for event in account_provider_stream(
                stream,
                provider="ensemble",
                model="aggregator",
            )
        ]

    assert len(events) == 1
    assert sink.finalized == []
    assert [reason for _, reason in sink.unknown] == [
        "provider_error:incomplete_stream"
    ]


def test_runtime_subagent_inherits_sink_with_distinct_execution() -> None:
    sink = _RecordingSink()
    parent = Agent(
        provider=_ErrorProvider(),
        config=AgentConfig(),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    child = parent._make_child_agent(SubagentSpec(task="child"), depth=1)

    assert child._usage_event_sink is sink
    child_context = child._usage_execution_context
    assert child_context is not None
    assert child_context.execution_id != "turn-1"
    assert child_context.parent_turn_id == "turn-1"
    assert child_context.session_id == "session-1"
    assert child_context.session_epoch == 7
    assert child_context.run_kind == "subagent"


@pytest.mark.asyncio
async def test_direct_meta_llm_helper_records_usage_with_parent_attribution() -> None:
    sink = _RecordingSink()
    provider = _DoneProvider(sink)
    chat = make_llm_chat_from_provider(
        provider=provider,
        base_config=AgentConfig(provider_id="fake", model_id="model-a"),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    result = await chat("system", "user")

    assert result == "ok"
    assert len(sink.started) == 1
    call = sink.started[0]
    assert call.execution_id != "turn-1"
    assert call.parent_turn_id == "turn-1"
    assert call.session_id == "session-1"
    assert call.session_epoch == 7
    assert call.run_kind == "meta_llm"
    assert len(sink.finalized) == 1
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_selector_fallback_accounts_each_physical_leg_without_outer_duplicate() -> None:
    sink = _RecordingSink()
    fallback = _CorrelationCapturingPhysicalLegProvider(
        "anthropic",
        [
            ProviderText(text="fallback"),
            ProviderDone(
                input_tokens=7,
                output_tokens=2,
                billed_cost=0.25,
                model="fallback-model",
            ),
        ],
    )
    primary = _CorrelationCapturingPhysicalLegProvider(
        "openai",
        [ProviderError(message="rate limited", code="429")],
    )
    wrapper = _SelectorFallbackProvider(primary, _FallbackSelector(fallback))
    tracker = _RecordingTracker()
    agent = Agent(
        provider=wrapper,
        config=AgentConfig(
            max_iterations=1,
            max_provider_retries=0,
            provider_id="openai",
            model_id="primary-model",
        ),
        usage_tracker=tracker,
        session_key="agent:main:test",
        usage_event_sink=sink,
        usage_execution_context=_context(),
        provider_request_correlation=ProviderRequestCorrelation(
            session_id="session-1",
            turn_id="turn-1",
            execution_id="execution-1",
            call_kind="agent.chat",
        ),
    )

    async for _ in agent.run_turn("hello"):
        pass

    assert primary.calls == 1
    assert fallback.calls == 1
    assert [(call.call_index, call.provider, call.model) for call in sink.started] == [
        (1, "openai", "primary-model"),
        (2, "anthropic", "fallback-model"),
    ]
    assert [(call.call_index, reason) for call, reason in sink.unknown] == [
        (1, "provider_error:429")
    ]
    assert [call.call_index for call, _ in sink.finalized] == [2]
    assert len(sink.started) == 2  # no Agent-level wrapper envelope
    assert tracker.rows[0][1]["provider"] == "anthropic"
    assert tracker.rows[0][1]["model_id"] == "fallback-model"
    expected_correlation = ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="execution-1",
        call_kind="agent.chat",
    )
    assert primary.configs[0].provider_request_correlation == expected_correlation
    assert fallback.configs[0].provider_request_correlation == ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="execution-1",
        call_kind="agent.chat.provider_fallback",
    )


@pytest.mark.asyncio
async def test_selector_wrapper_without_ledger_scope_is_streaming_compatible() -> None:
    fallback = _PhysicalLegProvider(
        "anthropic",
        [ProviderText(text="ok"), ProviderDone(model="fallback-model")],
    )
    primary = _PhysicalLegProvider(
        "openai",
        [ProviderError(message="rate limited", code="429")],
    )
    wrapper = _SelectorFallbackProvider(primary, _FallbackSelector(fallback))

    events = [event async for event in wrapper.chat([])]

    assert [getattr(event, "kind", "") for event in events] == ["text_delta", "done"]
    assert primary.calls == fallback.calls == 1


@pytest.mark.asyncio
async def test_meta_helper_with_selector_records_only_physical_legs() -> None:
    sink = _RecordingSink()
    fallback = _PhysicalLegProvider(
        "anthropic",
        [ProviderText(text="ok"), ProviderDone(model="fallback-model")],
    )
    primary = _PhysicalLegProvider(
        "openai",
        [ProviderError(message="rate limited", code="429")],
    )
    wrapper = _SelectorFallbackProvider(primary, _FallbackSelector(fallback))
    chat = make_llm_chat_from_provider(
        provider=wrapper,
        base_config=AgentConfig(provider_id="openai", model_id="primary-model"),
        usage_event_sink=sink,
        usage_execution_context=_context(),
    )

    assert await chat("system", "user") == "ok"

    assert [(call.call_index, call.provider, call.model) for call in sink.started] == [
        (1, "openai", "primary-model"),
        (2, "anthropic", "fallback-model"),
    ]
    assert {call.execution_id for call in sink.started} != {"turn-1"}
    assert all(call.run_kind == "meta_llm" for call in sink.started)
    assert len(sink.finalized) == 1
    assert len(sink.unknown) == 1


@pytest.mark.asyncio
async def test_shared_turn_scope_keeps_helper_and_agent_call_indices_unique() -> None:
    sink = _RecordingSink()
    context = _context()
    scope = UsageAccountingScope(sink=sink, context=context)
    helper_provider = _SequenceProvider([[ProviderDone(model="gate-model")]])
    agent_provider = _SequenceProvider(
        [[ProviderText(text="ok"), ProviderDone(model="agent-model")]]
    )
    agent = Agent(
        provider=agent_provider,
        config=AgentConfig(max_iterations=1, model_id="agent-model"),
        usage_event_sink=sink,
        usage_execution_context=context,
    )

    with bind_usage_accounting_scope(scope):
        _ = [
            event
            async for event in account_provider_stream(
                lambda: helper_provider.chat([]),
                provider="gate-provider",
                model="gate-model",
            )
        ]
        async for _ in agent.run_turn("hello"):
            pass

    assert [call.call_index for call in sink.started] == [1, 2]
    assert len({call.event_id for call in sink.started}) == 2


@pytest.mark.asyncio
async def test_turn_runner_preserves_retryable_ledger_start_error_code() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:usage-start-failure"
    await manager.create(session_key)
    sink = _UnavailableSink()
    provider = _SequenceProvider([[ProviderDone()]])
    runner = TurnRunner(
        provider_selector=_SingleProviderSelector(provider),
        session_manager=manager,
        usage_event_sink=sink,
    )
    try:
        events = [
            event
            async for event in runner.run(
                "hello",
                session_key,
                tool_context=ToolContext(
                    session_key=session_key,
                    caller_kind=CallerKind.CLI,
                ),
                history_has_persisted_user=False,
                no_memory_capture=True,
            )
        ]
    finally:
        await storage.close()

    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].code == UsageAccountingUnavailableError.code
    assert provider.calls == 0
