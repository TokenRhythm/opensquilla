"""Real call-chain admission after auxiliary 429/503, with no network."""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
import pytest

from opensquilla.compaction_timing import CompactionOperationTimeoutError
from opensquilla.contracts.turn_execution import StickyExecutionRole
from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.engine.types import CompactionEvent
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
)
from opensquilla.provider import retry_after
from opensquilla.provider.ensemble import (
    EnsembleMemberConfig,
    EnsembleProvider,
    _provider_stream_with_lifecycle,
)
from opensquilla.provider.failures import ProviderFailureKind, classify_provider_error
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.protocol import ProviderConnectionConfig, ProviderMetadata
from opensquilla.provider.retry_after import (
    ProviderRetryAfterCooldowns,
    RetryAfterDeferredError,
    RetryAfterWaitTimeoutError,
    provider_retry_after_scope,
    record_provider_retry_after,
    resolve_retry_delay_seconds,
)
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import ChatConfig, DoneEvent, ErrorEvent, Message, TextDeltaEvent
from opensquilla.session.compaction import call_compaction_provider
from opensquilla.session.compaction_deployment import (
    CompactionExecutionPlan,
    CompactionExecutionTarget,
)


class Clock:
    def __init__(self) -> None:
        # Keep synthetic integer waits exactly representable while remaining
        # aligned with asyncio deadlines, at most one second ahead of its clock.
        # Real deadline-expiration tests use an independent real-clock registry.
        self.now = float(math.ceil(time.monotonic()))
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def cooling(monkeypatch):
    clock = Clock()
    registry = ProviderRetryAfterCooldowns(clock=clock, sleep=clock.sleep)
    monkeypatch.setattr(retry_after, "_provider_retry_after_cooldowns", registry)
    monkeypatch.setattr("opensquilla.engine.agent.sleep_before_retry", clock.sleep)
    monkeypatch.setattr("opensquilla.engine.runtime.sleep_before_retry", clock.sleep)
    return registry, clock


class PhysicalProvider:
    provider_name = "openai"

    def __init__(self, streams=None, **identity):
        self.connection = ProviderConnectionConfig(
            provider_kind="openai",
            model="actual-model",
            api_key="test-opaque-key",
            base_url="https://retry-admission.test/v1",
            org_id="test-org",
            retry_after_scope_known=True,
        )
        self.connection = replace(self.connection, **identity)
        self.streams = streams or [[TextDeltaEvent(text="ok"), DoneEvent(stop_reason="stop")]]
        self.calls: list[Any] = []

    def provider_connection_config(self):
        return self.connection

    def provider_metadata(self):
        return ProviderMetadata(
            provider_name="openai",
            provider_kind="openai",
            provider_id="openai",
            model=self.connection.model,
            base_url=self.connection.base_url,
        )

    def project_final_request(self, messages, tools=None, config=None, **kwargs):
        connection = self.connection
        return OpenAIProvider(
            api_key=connection.api_key,
            model=connection.model,
            base_url=connection.base_url,
            org_id=connection.org_id,
        ).project_final_request(messages, tools, config, **kwargs)

    async def chat(self, messages, tools=None, config=None):
        index = len(self.calls)
        self.calls.append((messages, tools, config))
        for event in self.streams[min(index, len(self.streams) - 1)]:
            yield event


def plan(provider, reporter=None):
    return CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=provider,
                provider_id="openai",
                model="actual-model",
                context_window_tokens=128_000,
                max_output_tokens=4096,
                credential_pool_provider="openai",
                credential_pool_session_key="test-session",
                credential_pool_failure_reporter=reporter,
            ),
        )
    )


@dataclass
class Sink:
    starts: list[Any] = field(default_factory=list)
    finalized: list[Any] = field(default_factory=list)
    unknown: list[Any] = field(default_factory=list)

    async def start(self, call):
        self.starts.append(call)

    async def finalize(self, call, result):
        self.finalized.append((call, result))

    async def mark_unknown(self, call, reason):
        self.unknown.append((call, reason))


def usage(sink):
    return UsageAccountingScope(
        sink=sink,
        context=UsageExecutionContext(execution_id="retry-test", agent_run_id="retry-test"),
    )


def agent(provider, sink=None, **config):
    return Agent(
        provider,
        AgentConfig(
            timeout=60,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            **config,
        ),
        usage_event_sink=sink,
    )


def test_new_adapters_share_only_complete_actual_identity(cooling):
    registry, _clock = cooling
    original = PhysicalProvider()
    record_provider_retry_after(original, ErrorEvent(code="429", retry_after_s=7))
    clone = PhysicalProvider()
    assert registry.remaining(clone, scope=provider_retry_after_scope(clone)) == 7
    for changed in (
        {"api_key": "other-key"},
        {"model": "other-model"},
        {"org_id": "other-org"},
        {"base_url": "https://other.test/v1"},
        {"provider_kind": "other-adapter"},
    ):
        different = PhysicalProvider(**changed)
        assert registry.remaining(different, scope=provider_retry_after_scope(different)) == 0
    assert "test-opaque-key" not in repr(provider_retry_after_scope(original))
    assert "test-opaque-key" not in repr(registry._entries)


def test_unknown_unhashable_nonweakref_owner_is_local_and_expires(cooling):
    registry, clock = cooling
    first: list[Any] = []
    other: list[Any] = []
    registry.record(first, 3)
    assert registry.remaining(first) == 3
    assert registry.remaining(other) == 0
    clock.now += 3
    assert registry.remaining(first) == 0
    assert not registry._entries


def test_shorter_later_hint_cannot_shorten_cooling(cooling):
    registry, clock = cooling
    provider = []
    registry.record(provider, 10)
    clock.now += 2
    registry.record(provider, 1)
    assert registry.remaining(provider) == 8
    registry.record(provider, 20)
    assert registry.remaining(provider) == 20


def test_longer_deadline_retains_its_own_failure_reason(cooling):
    registry, clock = cooling
    provider = PhysicalProvider()
    record_provider_retry_after(provider, ErrorEvent(code="503", retry_after_s=100))
    clock.now += 1
    record_provider_retry_after(provider, ErrorEvent(code="429", retry_after_s=1))
    scope = provider_retry_after_scope(provider)
    assert registry.remaining(provider, scope=scope) == 99
    assert registry.reason(provider, scope=scope) == "provider_overloaded"


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan, -1, "invalid", True, None])
def test_malformed_hint_does_not_create_permanent_process_tombstone(cooling, bad):
    registry, _clock = cooling
    provider = PhysicalProvider()
    registry.record(provider, bad, scope=provider_retry_after_scope(provider))
    assert not registry._entries
    if bad == math.inf:
        assert resolve_retry_delay_seconds(local_delay_s=0, provider_retry_after_s=bad) is None


async def test_shared_wait_is_remaining_time_and_budget_is_not_restarted(cooling):
    registry, clock = cooling
    provider = []
    registry.record(provider, 10)
    clock.now += 4
    waits = [x async for x in registry.wait(provider, deadline_at_monotonic=clock.now + 20)]
    assert waits == [6]
    assert clock.sleeps == [6]
    assert not [x async for x in registry.wait(provider, deadline_at_monotonic=clock.now + 20)]


async def test_long_hint_refuses_without_shortening_and_later_caller_retains_it(cooling):
    registry, clock = cooling
    provider = PhysicalProvider()
    scope = provider_retry_after_scope(provider)
    registry.record(provider, 700, scope=scope)
    with pytest.raises(RetryAfterDeferredError, match="deadline"):
        async for _ in registry.wait(
            provider,
            scope=scope,
            deadline_at_monotonic=clock.now + 600,
        ):
            pytest.fail("no wait is affordable")
    assert not clock.sleeps
    assert registry.remaining(provider, scope=scope) == 700
    waits = [
        x
        async for x in registry.wait(
            provider,
            scope=scope,
            deadline_at_monotonic=clock.now + 800,
        )
    ]
    assert waits == [700]


async def test_wait_rechecks_concurrent_extension_and_never_clears_on_success(cooling):
    registry, clock = cooling
    provider = []
    registry.record(provider, 2)

    async def advance_then_extend(seconds):
        await clock.sleep(seconds)
        if len(clock.sleeps) == 1:
            registry.record(provider, 3)

    waits = [
        x
        async for x in registry.wait(
            provider,
            deadline_at_monotonic=clock.now + 10,
            sleep=advance_then_extend,
        )
    ]
    assert waits == [2, 3]


async def test_cancel_during_wait_never_consumes_cooldown(cooling):
    registry, clock = cooling
    provider = []
    registry.record(provider, 2)

    async def cancel(_seconds):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        async for _ in registry.wait(
            provider,
            deadline_at_monotonic=clock.now + 10,
            sleep=cancel,
        ):
            pass
    assert registry.remaining(provider) == 2


async def test_internal_timeout_is_not_parent_expiration(cooling):
    registry, clock = cooling
    provider = []
    registry.record(provider, 2)
    internal = TimeoutError("internal synthetic timeout")

    async def fail(_seconds):
        raise internal

    with pytest.raises(TimeoutError) as caught:
        async for _ in registry.wait(
            provider,
            deadline_at_monotonic=clock.now + 10,
            sleep=fail,
        ):
            pass
    assert caught.value is internal
    assert not isinstance(caught.value, RetryAfterWaitTimeoutError)


async def test_actual_parent_deadline_expires_during_wait():
    registry = ProviderRetryAfterCooldowns()
    provider = []
    registry.record(provider, 0.005)

    async def blocked(_seconds):
        await asyncio.Event().wait()

    with pytest.raises(RetryAfterWaitTimeoutError):
        async for _ in registry.wait(
            provider,
            deadline_at_monotonic=time.monotonic() + 0.025,
            sleep=blocked,
        ):
            pass


@pytest.mark.parametrize("status", ["429", "503"])
async def test_summary_failure_then_new_direct_agent_waits_and_dispatches_once(cooling, status):
    _registry, clock = cooling
    summary_provider = PhysicalProvider([[ErrorEvent(code=status, retry_after_s=3)]])
    assert await call_compaction_provider("old history", "", plan(summary_provider)) is None
    normal_provider = PhysicalProvider()
    sink = Sink()
    events = [e async for e in agent(normal_provider, sink).run_turn("Continue")]
    assert clock.sleeps == [3]
    assert len(summary_provider.calls) == len(normal_provider.calls) == len(sink.starts) == 1
    assert any(e.kind == "done" for e in events)
    reason = "rate_limited" if status == "429" else "provider_overloaded"
    assert any(e.kind == "provider_activity" and e.reason == reason for e in events)


@pytest.mark.parametrize("status", [429, 503])
async def test_native_header_summary_to_fresh_native_agent_and_org_isolation(
    cooling,
    monkeypatch,
    status,
):
    _registry, clock = cooling
    calls = []

    def handle(request):
        calls.append((request.headers.get("OpenAI-Organization"), clock.now))
        if len(calls) == 1:
            return httpx.Response(status, headers={"Retry-After": "2"}, text="temporary")
        frames = [
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        ]
        body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(body + "data: [DONE]\n\n").encode(),
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(handle)}),
    )

    def native(org):
        return OpenAIProvider(
            api_key="synthetic-header-test",
            model="actual-model",
            base_url="https://native-retry.test/v1",
            org_id=org,
        )

    assert await call_compaction_provider("old history", "", plan(native("org-a"))) is None
    assert not clock.sleeps
    other_events = [e async for e in agent(native("org-b")).run_turn("continue")]
    assert any(e.kind == "done" for e in other_events)
    assert not clock.sleeps
    same_events = [e async for e in agent(native("org-a")).run_turn("continue")]
    assert any(e.kind == "done" for e in same_events)
    assert clock.sleeps == [2]
    assert [org for org, _when in calls] == ["org-a", "org-b", "org-a"]
    assert calls[2][1] - calls[0][1] >= 2


async def test_manual_failure_blocks_preflight_and_main_without_usage_or_dispatch(cooling):
    registry, clock = cooling
    provider = PhysicalProvider([[ErrorEvent(code="429", retry_after_s=700)]])
    assert await call_compaction_provider("old", "", plan(provider)) is None
    clone = PhysicalProvider()
    sink = Sink()
    starts = []
    with bind_usage_accounting_scope(usage(sink)):
        result = await call_compaction_provider(
            "old",
            "",
            plan(clone),
            deadline_at_monotonic=clock.now + 600,
            on_summary_call_started=lambda: starts.append(True),
        )
    assert result is None
    events = [e async for e in agent(clone, sink).run_turn("Continue")]
    assert any(e.kind == "error" and e.code == "provider_retry_after_deadline" for e in events)
    assert not clone.calls and not starts and not sink.starts and not sink.unknown
    assert registry.remaining(clone, scope=provider_retry_after_scope(clone)) == 700
    assert not clock.sleeps


async def test_existing_normal_retry_does_not_wait_twice(cooling):
    _registry, clock = cooling
    provider = PhysicalProvider(
        [
            [ErrorEvent(code="429", retry_after_s=3)],
            [TextDeltaEvent(text="ok"), DoneEvent(stop_reason="stop")],
        ]
    )
    events = [e async for e in agent(provider).run_turn("Continue")]
    assert clock.sleeps == [3]
    assert len(provider.calls) == 2
    assert any(e.kind == "done" for e in events)


@pytest.mark.parametrize("bad", ["raises", "wrong_fields"])
async def test_invalid_optional_metadata_does_not_break_normal_chat(cooling, bad):
    class BadMetadata(PhysicalProvider):
        def provider_connection_config(self):
            if bad == "raises":
                raise ValueError("bad optional metadata")
            return ProviderConnectionConfig(provider_kind=123, retry_after_scope_known=True)

    provider = BadMetadata()
    assert provider_retry_after_scope(provider) is None
    events = [e async for e in agent(provider).run_turn("Continue")]
    assert len(provider.calls) == 1
    assert any(e.kind == "done" for e in events)


async def test_selector_physical_gate_waits_before_usage(cooling, monkeypatch):
    _registry, clock = cooling
    summary = PhysicalProvider([[ErrorEvent(code="429", retry_after_s=3)]])
    assert await call_compaction_provider("old", "", plan(summary)) is None
    physical = PhysicalProvider()
    monkeypatch.setattr("opensquilla.provider.selector._build_provider", lambda _config: physical)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openai",
                model="requested-override",
                api_key="different-requested-key",
            )
        )
    )
    wrapper = _SelectorFallbackProvider(selector.resolve(), selector)
    sink = Sink()
    with bind_usage_accounting_scope(usage(sink)):
        events = [
            e
            async for e in wrapper.chat(
                [Message(role="user", content="continue")],
                config=ChatConfig(turn_deadline_at_monotonic=clock.now + 10),
            )
        ]
    assert clock.sleeps == [3]
    assert len(physical.calls) == len(sink.starts) == 1
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.parametrize("provider_name", ["anthropic", "kimi_coding_anthropic"])
@pytest.mark.parametrize(("hint", "deadline_seconds"), [(700, 600), (901, None)])
async def test_selector_overload_cooldown_uses_independent_fallback(
    cooling, monkeypatch, provider_name, hint, deadline_seconds,
):
    _registry, clock = cooling

    class AnthropicPhysicalProvider(PhysicalProvider):
        def provider_metadata(self):
            return replace(
                super().provider_metadata(),
                provider_name=provider_name,
                provider_kind="anthropic",
                provider_id=provider_name,
            )

    primary = AnthropicPhysicalProvider(provider_kind="anthropic", model="primary")
    primary.provider_name = provider_name
    fallback = PhysicalProvider(model="fallback", base_url="https://fallback.test/v1")
    record_provider_retry_after(primary, ErrorEvent(code="503", retry_after_s=hint))
    monkeypatch.setattr(
        "opensquilla.provider.selector._build_provider",
        lambda cfg: primary if cfg.model == "primary" else fallback,
    )
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(provider=provider_name, model="primary", api_key="synthetic"),
            fallbacks=[ProviderConfig(provider="openai", model="fallback", api_key="synthetic")],
        )
    )
    wrapper = _SelectorFallbackProvider(selector.resolve(), selector)
    events = [
        event
        async for event in wrapper.chat(
            [Message(role="user", content="continue")],
            config=ChatConfig(
                turn_deadline_at_monotonic=(
                    clock.now + deadline_seconds if deadline_seconds is not None else None
                ),
            ),
        )
    ]

    assert primary.calls == []
    assert len(fallback.calls) == 1
    assert clock.sleeps == []
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert isinstance(events[-1], DoneEvent)
    assert any(
        event.kind == "provider_activity"
        and event.phase == "fallback"
        and event.reason == "provider_overloaded"
        for event in events
    )


async def test_ensemble_lifecycle_gate_excludes_provider_timeout_and_start(cooling):
    registry, clock = cooling
    provider = PhysicalProvider()
    registry.record(provider, 7, scope=provider_retry_after_scope(provider))
    starts = []

    async def started():
        starts.append(True)

    events = [
        e
        async for e in _provider_stream_with_lifecycle(
            lambda: provider.chat([Message(role="user", content="continue")]),
            execution_context=None,
            role=StickyExecutionRole.PRIMARY_AGGREGATOR,
            logical_call_index=0,
            attempt_index=0,
            owner="cooldown-test",
            phase="ensemble_aggregator_wait",
            message="waiting",
            timeout_seconds=0.001,
            reset_deadline_on_event=False,
            on_request_start=started,
            physical_provider=provider,
            physical_config=ChatConfig(turn_deadline_at_monotonic=clock.now + 20),
        )
    ]
    assert clock.sleeps == [7]
    assert len(starts) == len(provider.calls) == 1
    assert isinstance(events[-1], DoneEvent)


async def test_ensemble_refusal_never_starts_physical_or_lifecycle(cooling):
    registry, clock = cooling
    provider = PhysicalProvider()
    registry.record(provider, 700, scope=provider_retry_after_scope(provider))
    starts = []

    async def started():
        starts.append(True)

    events = [
        e
        async for e in _provider_stream_with_lifecycle(
            lambda: provider.chat([]),
            execution_context=None,
            role=StickyExecutionRole.PRIMARY_AGGREGATOR,
            logical_call_index=0,
            attempt_index=0,
            owner="cooldown-test",
            phase="ensemble_aggregator_wait",
            message="waiting",
            timeout_seconds=0.001,
            reset_deadline_on_event=False,
            on_request_start=started,
            physical_provider=provider,
            physical_config=ChatConfig(turn_deadline_at_monotonic=clock.now + 600),
        )
    ]
    assert len(events) == 1 and events[0].code == "provider_retry_after_deadline"
    assert not starts and not provider.calls and not clock.sleeps


async def test_summary_expired_frame_preserves_real_hint(cooling, monkeypatch):
    registry, _clock = cooling

    class Expired:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def observe(self, _event):
            raise CompactionOperationTimeoutError("test same-frame expiration")

    monkeypatch.setattr(
        "opensquilla.session.compaction.compaction_progress_timeout", lambda **_: Expired()
    )
    provider = PhysicalProvider([[ErrorEvent(code="429", retry_after_s=3)]])
    with pytest.raises(CompactionOperationTimeoutError):
        await call_compaction_provider("old", "", plan(provider))
    assert registry.remaining(provider, scope=provider_retry_after_scope(provider)) == 3


@pytest.mark.parametrize("style", ["legacy", "hint", "raises_type_error"])
async def test_credential_reporter_is_called_once_with_supported_signature(cooling, style):
    calls = []

    def legacy(provider, session, kind):
        calls.append((provider, session, kind))
        if style == "raises_type_error":
            raise TypeError("callback body error")

    def modern(provider, session, kind, *, retry_after_seconds=None):
        calls.append((provider, session, kind, retry_after_seconds))

    provider = PhysicalProvider([[ErrorEvent(code="429", retry_after_s=4)]])
    assert (
        await call_compaction_provider(
            "old",
            "",
            plan(
                provider,
                modern if style == "hint" else legacy,
            ),
        )
        is None
    )
    assert len(calls) == 1
    if style == "hint":
        assert calls[0][-1] == 4


@pytest.mark.parametrize("status", ["429", "503"])
@pytest.mark.parametrize("hint", [700, 901])
async def test_deferred_reason_retains_status_and_actual_bound(cooling, status, hint):
    registry, clock = cooling
    provider = PhysicalProvider()
    record_provider_retry_after(provider, ErrorEvent(code=status, retry_after_s=hint))
    deadline = clock.now + 600 if hint == 700 else None
    with pytest.raises(RetryAfterDeferredError) as caught:
        async for _ in registry.wait(
            provider,
            scope=provider_retry_after_scope(provider),
            deadline_at_monotonic=deadline,
        ):
            pytest.fail("unaffordable wait")
    error = caught.value
    assert ("deadline" in error.code) == (hint == 700)
    expected = (
        ProviderFailureKind.RATE_LIMITED
        if status == "429"
        else ProviderFailureKind.PROVIDER_OVERLOADED
    )
    assert classify_provider_error("openai", None, raw_code=error.code) == expected
    assert registry.remaining(provider, scope=provider_retry_after_scope(provider)) == hint
    assert not clock.sleeps


async def test_summary_failed_aggregator_cooldown_survives_fresh_ensemble_adapter(
    cooling,
    monkeypatch,
):
    _registry, clock = cooling
    summary = PhysicalProvider([[ErrorEvent(code="429", retry_after_s=4)]])
    assert await call_compaction_provider("old", "", plan(summary)) is None
    proposer = PhysicalProvider(model="proposer")
    aggregator = PhysicalProvider()
    monkeypatch.setattr(
        "opensquilla.provider.ensemble._build_provider",
        lambda config: proposer if config.model == "proposer" else aggregator,
    )
    member = lambda model: EnsembleMemberConfig(  # noqa: E731
        label=model,
        provider_config=ProviderConfig(
            provider="openai",
            model=model,
            api_key="test-opaque-key",
            base_url="https://retry-admission.test/v1",
            org_id="test-org",
        ),
    )
    ensemble = EnsembleProvider(
        profile_name="test",
        proposers=[member("proposer")],
        aggregator=member("actual-model"),
        proposer_timeout_seconds=1,
        aggregator_timeout_seconds=1,
    )
    sink = Sink()
    with bind_usage_accounting_scope(usage(sink)):
        events = [
            e
            async for e in ensemble.chat(
                [Message(role="user", content="Continue")],
                config=ChatConfig(turn_deadline_at_monotonic=clock.now + 30),
            )
        ]
    assert clock.sleeps == [4], [
        (type(e).__name__, getattr(e, "code", ""), getattr(e, "message", "")) for e in events
    ]
    assert len(proposer.calls) == len(aggregator.calls) == 1
    assert len(sink.starts) == 2
    assert any(isinstance(e, DoneEvent) for e in events)


@pytest.mark.parametrize("mode", ["unaffordable", "expires", "parent", "internal"])
async def test_pending_candidate_cooling_has_correct_deadline_owner(monkeypatch, mode):
    cooling_now = time.monotonic()
    registry = ProviderRetryAfterCooldowns(clock=lambda: cooling_now)
    monkeypatch.setattr(retry_after, "_provider_retry_after_cooldowns", registry)
    provider = PhysicalProvider()
    sink = Sink()
    runtime = agent(provider, sink)
    runtime._session_key = "retry-pending-test"
    notifications = []
    monkeypatch.setattr(
        "opensquilla.engine.agent.notify_compaction",
        lambda _, **event: notifications.append(event),
    )
    if mode == "parent":
        runtime.config.timeout = 0.025

    def stage(**_kwargs):
        nonlocal cooling_now
        install_deadline = time.monotonic() + (10 if mode == "parent" else 0.025)
        request_context = runtime._compaction_request_context
        assert request_context is not None
        parent_deadline = request_context.chat_config.turn_deadline_at_monotonic
        assert parent_deadline is not None
        # Freeze only cooldown bookkeeping inside the intended admission window;
        # scheduling must not expire the 5ms hint before the gate is exercised.
        # asyncio.timeout_at still enforces the unmodified real owner deadline.
        cooling_now = min(parent_deadline, install_deadline) - 0.025
        runtime._pending_durable_compaction_event = CompactionEvent(
            compaction_id="existing-operation",
            summary="Completed but uninstalled checkpoint",
            compaction_deadline_at_monotonic=install_deadline,
        )
        record_provider_retry_after(
            provider,
            ErrorEvent(
                code="429",
                retry_after_s=1 if mode == "unaffordable" else 0.005,
            ),
        )

    waits = []

    async def wait(seconds):
        waits.append(seconds)
        if mode == "internal":
            raise TimeoutError("independent wait failure")
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "_record_provider_tool_schema_event", stage)
    monkeypatch.setattr("opensquilla.engine.agent.sleep_before_retry", wait)
    events = [e async for e in runtime.run_turn("Continue")]
    error_codes = [e.code for e in events if e.kind == "error"]
    assert error_codes == [
        {
            "unaffordable": "provider_retry_after_deadline",
            "expires": "compaction_deadline_exceeded",
            "parent": "agent_runtime_timeout",
            "internal": "agent_runtime_timeout",
        }[mode]
    ]
    assert not provider.calls and not sink.starts and not sink.unknown
    assert len(waits) == (0 if mode == "unaffordable" else 1)
    assert not any(isinstance(e, CompactionEvent) for e in events)
    assert runtime._pending_durable_compaction_event is None
    terminal = [e for e in notifications if e.get("status") in {"failed", "timed_out"}]
    assert len(terminal) == 1
    assert terminal[0]["compaction_id"] == "existing-operation"
    assert terminal[0]["applied"] is False
    assert terminal[0]["status"] == ("timed_out" if mode == "expires" else "failed")


@pytest.mark.parametrize("finish", ["close", "cancel", "exhaust"])
async def test_dispatch_observer_resets_each_pull_and_closes_children(finish):
    from opensquilla.provider.retry_after import (
        RetryAfterDispatchEvidence,
        mark_retry_after_physical_start,
        observe_retry_after_dispatch,
    )

    evidence = RetryAfterDispatchEvidence()
    closed = []
    entered = asyncio.Event()

    async def stream():
        try:
            # Child tasks inherit one mutable object rather than a new counter.
            async def child():
                mark_retry_after_physical_start()

            await asyncio.create_task(child())
            yield "first"
            assert retry_after._dispatch_evidence.get() is evidence
            if finish == "cancel":
                entered.set()
                await asyncio.Event().wait()
        finally:
            closed.append(True)

    wrapped = observe_retry_after_dispatch(stream(), evidence)
    assert await anext(wrapped) == "first"
    assert evidence.physical_started
    assert retry_after._dispatch_evidence.get() is None
    if finish == "cancel":
        task = asyncio.create_task(anext(wrapped))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif finish == "exhaust":
        with pytest.raises(StopAsyncIteration):
            await anext(wrapped)
    await wrapped.aclose()
    assert closed == [True]
    assert retry_after._dispatch_evidence.get() is None
    fresh = RetryAfterDispatchEvidence()
    assert not fresh.physical_started and not fresh.cooling_observed


@pytest.mark.parametrize("kind", ["selector_fallback", "ensemble_aggregator"])
async def test_dispatch_evidence_never_resets_after_an_earlier_leaf(cooling, monkeypatch, kind):
    from opensquilla.provider.retry_after import (
        RetryAfterDispatchEvidence,
        observe_retry_after_dispatch,
    )

    registry, clock = cooling
    first = PhysicalProvider(
        [[ErrorEvent(code="503", message="unavailable")]] if kind == "selector_fallback" else None,
        model="first",
    )
    later = PhysicalProvider(model="later")
    registry.record(later, 4, scope=provider_retry_after_scope(later))

    def build(cfg):
        return first if cfg.model == "first" else later

    def member(model):
        return ProviderConfig(provider="openai", model=model, api_key="synthetic")

    if kind == "selector_fallback":
        monkeypatch.setattr("opensquilla.provider.selector._build_provider", build)
        selector = ModelSelector(
            SelectorConfig(primary=member("first"), fallbacks=[member("later")])
        )
        wrapper = _SelectorFallbackProvider(selector.resolve(), selector)
        wrapper._retry_policy.max_retries = 0
    else:
        monkeypatch.setattr("opensquilla.provider.ensemble._build_provider", build)
        wrapper = EnsembleProvider(
            profile_name="evidence",
            proposers=[EnsembleMemberConfig(label="first", provider_config=member("first"))],
            aggregator=EnsembleMemberConfig(label="later", provider_config=member("later")),
        )
    evidence = RetryAfterDispatchEvidence()
    sink = Sink()
    with bind_usage_accounting_scope(usage(sink)):
        stream = observe_retry_after_dispatch(
            wrapper.chat(
                [Message(role="user", content="Continue")],
                config=ChatConfig(turn_deadline_at_monotonic=clock.now + 30),
            ),
            evidence,
        )
        events = []
        async for event in stream:
            events.append(event)
            if evidence.cooling_observed:
                assert evidence.physical_started
                assert len(sink.starts) >= 1
    assert evidence.cooling_observed and evidence.physical_started
    assert len(first.calls) == len(later.calls) == 1
    assert len(sink.starts) == 2
    assert any(isinstance(e, DoneEvent) for e in events)
    assert retry_after._dispatch_evidence.get() is None


async def test_agent_ensemble_zero_dispatch_cooling_fails_closed_without_exact_projection(
    monkeypatch,
):
    from opensquilla.engine.agent import _PendingCompactionSource
    from opensquilla.session.compaction import CompactionConfig

    registry = ProviderRetryAfterCooldowns()
    monkeypatch.setattr(retry_after, "_provider_retry_after_cooldowns", registry)
    physical = PhysicalProvider()
    monkeypatch.setattr("opensquilla.provider.ensemble._build_provider", lambda _: physical)
    member = EnsembleMemberConfig(
        label="native",
        provider_config=ProviderConfig(
            provider="openai",
            model="actual-model",
            api_key="synthetic",
        ),
    )
    wrapper = EnsembleProvider(profile_name="pending", proposers=[member], aggregator=member)
    sink = Sink()
    runtime = agent(wrapper, sink)
    runtime._session_key = "ensemble-pending"
    notifications = []
    monkeypatch.setattr(
        "opensquilla.engine.agent.notify_compaction", lambda _, **event: notifications.append(event)
    )
    source = [
        Message(role="user", content="original canonical history"),
        Message(role="user", content="Continue"),
    ]
    recovery_calls = []
    recover = runtime._recover_progressed_request_window

    async def observe(*args, **kwargs):
        recovery_calls.append((args, kwargs))
        assert not physical.calls and not sink.starts
        return await recover(*args, **kwargs)

    def stage(**_kwargs):
        runtime._pending_durable_compaction_event = CompactionEvent(
            compaction_id="same-operation",
            summary="uninstalled",
            compaction_deadline_at_monotonic=time.monotonic() + 0.05,
        )
        runtime._pending_durable_compaction_source = _PendingCompactionSource(
            compaction_id="same-operation",
            messages=source,
            rejected_messages=source,
            request_context_insert_index=1,
            runtime_context_insert_index=1,
            protected_turn_start_index=1,
            config=CompactionConfig(operation_id="same-operation"),
        )
        record_provider_retry_after(physical, ErrorEvent(code="429", retry_after_s=0.2))

    monkeypatch.setattr(runtime, "_record_provider_tool_schema_event", stage)
    monkeypatch.setattr(runtime, "_recover_progressed_request_window", observe)
    before = runtime.history_snapshot()
    events = [e async for e in runtime.run_turn("Continue")]
    assert len(recovery_calls) == 1
    assert recovery_calls[0][0][0] == source
    assert not physical.calls and not sink.starts
    assert not any(isinstance(e, CompactionEvent) for e in events)
    assert [e.code for e in events if e.kind == "error"] == ["compaction_deadline_exceeded"]
    assert runtime.history_snapshot() == before
    assert runtime._pending_durable_compaction_source is None
    terminal = [
        e
        for e in notifications
        if e.get("status") in {"timed_out", "failed", "emergency_ephemeral"}
    ]
    assert len(terminal) == 1 and terminal[0]["status"] == "timed_out"
    assert terminal[0]["compaction_id"] == "same-operation"
    assert terminal[0]["applied"] is False
