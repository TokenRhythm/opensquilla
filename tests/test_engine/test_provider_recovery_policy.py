from __future__ import annotations

import pytest

from opensquilla.provider.failures import ProviderFailureKind
from opensquilla.provider.recovery import recovery_delay_seconds
from tests.test_engine.test_selector_provider_recovery import clock as clock


@pytest.mark.parametrize("release_s,new_times,old_success", [
    (5, [0, 10], True), (30, [0, 10, 30], False), (60, [0, 10, 30, 70], False),
])
async def test_rpm_window_comparison(monkeypatch, release_s, new_times, old_success):
    """Virtual RPM windows compare bounded attempts, not measured provider SLAs."""
    monkeypatch.setattr("opensquilla.provider.recovery.random.randint", lambda *_args: 0)
    now = 0.0
    dispatches = [now]
    for attempt in range(3):
        delay = recovery_delay_seconds(
            kind=ProviderFailureKind.RATE_LIMITED, attempt=attempt,
            provider_retry_after_s=None, now=now, deadline_at_monotonic=120,
        )
        assert delay is not None
        now += delay
        dispatches.append(now)
        if now >= release_s:
            break
    assert dispatches == new_times
    assert (7 >= release_s) is old_success  # Original 1/2/4 waits end at t=7.
    assert dispatches[-1] >= release_s


async def test_rpm_budget_declines_wait_and_does_not_slow_other_errors(monkeypatch):
    monkeypatch.setattr("opensquilla.provider.recovery.random.randint", lambda *_args: 0)
    assert recovery_delay_seconds(
        kind=ProviderFailureKind.RATE_LIMITED, attempt=2, provider_retry_after_s=None,
        now=30, deadline_at_monotonic=60,
    ) is None
    assert [recovery_delay_seconds(
        kind=ProviderFailureKind.PROVIDER_OVERLOADED, attempt=index,
        provider_retry_after_s=None, now=0,
    ) for index in range(3)] == [1, 2, 4]


async def test_trusted_rpm_hint_remains_floor_without_invented_minutes(monkeypatch):
    monkeypatch.setattr("opensquilla.provider.recovery.random.randint", lambda *_args: 0)
    assert recovery_delay_seconds(
        kind=ProviderFailureKind.RATE_LIMITED, attempt=0,
        provider_retry_after_s=35, now=0,
    ) == 35
    assert recovery_delay_seconds(
        kind=ProviderFailureKind.RATE_LIMITED, attempt=0,
        provider_retry_after_s=950, now=0,
    ) is None


async def test_connection_wait_is_admitted_against_deadline():
    assert recovery_delay_seconds(
        kind=ProviderFailureKind.TRANSPORT_TRANSIENT, attempt=0,
        provider_retry_after_s=None, connection_failed=True,
        now=0, deadline_at_monotonic=4,
    ) is None


@pytest.mark.parametrize("failures,expected_calls", [(1, 2), (3, 4), (4, 4)])
async def test_agent_and_selector_share_one_rpm_budget(
    monkeypatch, clock, failures, expected_calls,
):
    from opensquilla.engine import Agent, AgentConfig
    from opensquilla.provider.types import ErrorEvent
    from tests.test_engine.test_selector_provider_recovery import _success, _wrapper

    monkeypatch.setattr("opensquilla.provider.recovery.random.randint", lambda *_args: 0)
    wrapper, _health, calls = _wrapper(monkeypatch, {"primary": [
        *[[ErrorEvent(code="429", http_status=429, message="rate limit")]
          for _ in range(failures)], _success(),
    ]})
    events = [event async for event in Agent(wrapper, AgentConfig(timeout=180)).run_turn("run")]
    assert len(calls) == expected_calls
    waits = [event for event in events
             if event.kind == "provider_activity" and event.phase == "retry_wait"]
    assert [event.retry_after_ms for event in waits] == (
        [10_000, 20_000, 40_000][:expected_calls - 1]
    )
    assert all(event.http_status == 429 for event in waits)
    errors = [event for event in events if event.kind == "error"]
    assert bool(errors) is (failures == 4)
    if errors:
        assert errors[-1].http_status == 429


@pytest.mark.parametrize("provider_code", ["insufficient_quota", "usage_limit_reached"])
async def test_non_rpm_429_does_not_poison_shared_cooldown(clock, provider_code):
    from opensquilla.provider.retry_after import (
        provider_retry_after_cooldowns,
        record_provider_retry_after,
    )
    from opensquilla.provider.types import ErrorEvent

    provider = object()
    record_provider_retry_after(provider, ErrorEvent(
        code="429", http_status=429, provider_code=provider_code, retry_after_s=120,
    ))
    assert provider_retry_after_cooldowns().remaining(provider) == 0


async def test_expired_deadline_without_cooling_never_starts_factory(clock):
    from opensquilla.provider.recovery import retry_provider_stream
    from opensquilla.provider.retry_after import RetryAfterWaitTimeoutError
    from opensquilla.provider.types import ChatConfig

    calls = []

    async def stream():
        calls.append(True)
        yield None

    with pytest.raises(RetryAfterWaitTimeoutError):
        async for _event in retry_provider_stream(
            stream, provider=object(), model="test",
            config=ChatConfig(turn_deadline_at_monotonic=clock[0][0] - 1),
        ):
            pass
    assert calls == []


@pytest.mark.parametrize("origin", [0.0, 29.1, 255.9])
def test_cooldown_uses_exact_http_fact_independent_of_business_code(monkeypatch, origin):
    from opensquilla.provider import retry_after
    from opensquilla.provider.retry_after import (
        ProviderRetryAfterCooldowns,
        provider_retry_after_cooldowns,
        record_provider_retry_after,
    )
    from opensquilla.provider.types import ErrorEvent

    now = [origin]
    monkeypatch.setattr(
        retry_after, "_provider_retry_after_cooldowns",
        ProviderRetryAfterCooldowns(clock=lambda: now[0]),
    )
    provider = object()
    record_provider_retry_after(provider, ErrorEvent(
        code="upstream_rate_limit", http_status=429, retry_after_s=35,
    ))
    # Adding/subtracting a monotonic origin may round the duration by an ULP.
    assert provider_retry_after_cooldowns().remaining(provider) == pytest.approx(
        35, rel=0, abs=1e-12,
    )
    now[0] = origin + 34
    assert provider_retry_after_cooldowns().remaining(provider) > 0
    now[0] = origin + 35
    assert provider_retry_after_cooldowns().remaining(provider) == 0


async def test_raised_http_error_main_owner_preserves_retry_after(monkeypatch, clock):
    import httpx

    from opensquilla.engine.agent import Agent, AgentConfig
    from tests.test_engine.test_selector_provider_recovery import _Provider, _success, _wrapper

    original = _Provider.chat

    async def raise_once(self, messages, tools=None, config=None):
        if not self.calls:
            self.calls.append(self.model)
            response = httpx.Response(
                429, headers={"Retry-After": "35"},
                request=httpx.Request("POST", "https://test.invalid"),
            )
            response.raise_for_status()
        async for event in original(self, messages, tools=tools, config=config):
            yield event

    monkeypatch.setattr(_Provider, "chat", raise_once)
    monkeypatch.setattr("opensquilla.provider.recovery.random.randint", lambda *_args: 0)
    provider, _, calls = _wrapper(monkeypatch, {"primary": [_success()]})
    events = [event async for event in Agent(provider, AgentConfig(timeout=180)).run_turn("run")]
    assert calls == ["primary", "primary"]
    waits = [event for event in events
             if event.kind == "provider_activity" and event.phase == "retry_wait"]
    assert [(event.retry_after_ms, event.http_status) for event in waits] == [(35000, 429)]
    assert any(event.kind == "done" for event in events)


@pytest.mark.parametrize("provider_code,kind", [
    ("usage_limit_reached", "usage_limit"), ("insufficient_quota", "insufficient_credits"),
])
async def test_raised_non_rpm_429_is_terminal_without_cooling(
    monkeypatch, clock, provider_code, kind,
):
    import httpx

    from opensquilla.engine.agent import Agent, AgentConfig
    from opensquilla.provider.retry_after import provider_retry_after_cooldowns
    from tests.test_engine.test_selector_provider_recovery import _Provider, _success, _wrapper

    attempted = []

    async def fail(self, messages, tools=None, config=None):
        attempted.append(self)
        self.calls.append(self.model)
        response = httpx.Response(
            429, headers={"Retry-After": "35"}, json={"error": {"code": provider_code}},
            request=httpx.Request("POST", "https://test.invalid"),
        )
        response.raise_for_status()
        yield  # Make this the same async-iterator protocol as provider.chat.

    monkeypatch.setattr(_Provider, "chat", fail)
    provider, _, calls = _wrapper(monkeypatch, {"primary": [_success()]})
    events = [event async for event in Agent(provider, AgentConfig(timeout=180)).run_turn("run")]
    assert calls == ["primary"]
    assert clock[1] == []
    failures = [event for event in events if event.kind == "error"]
    assert len(failures) == 1
    assert (failures[0].failure_kind, failures[0].http_status) == (kind, 429)
    assert provider_retry_after_cooldowns().remaining(attempted[0]) == 0
