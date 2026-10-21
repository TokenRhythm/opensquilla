from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from opensquilla.engine.usage_accounting import bind_usage_accounting_scope
from opensquilla.provider import retry_after
from opensquilla.provider.types import DoneEvent, ErrorEvent, TextDeltaEvent
from opensquilla.session.compaction import (
    CompactionRequest,
    call_compaction_provider,
    compact_context,
)
from opensquilla.session.compaction_deployment import (
    CompactionExecutionPlan,
    CompactionExecutionTarget,
)
from tests.test_session.test_compaction_provider_runtime import (
    _entries,
    _Provider,
    _Sink,
    _Stream,
    _suffix_config,
    _usage_scope,
)


@pytest.fixture
async def clock(monkeypatch):
    loop = asyncio.get_running_loop()
    now = [loop.time()]
    delays = []
    real_sleep = asyncio.sleep

    async def sleep(delay):
        delays.append(delay)
        now[0] += delay
        await real_sleep(0)

    monkeypatch.setattr(loop, "time", lambda: now[0])
    monkeypatch.setattr("opensquilla.session.compaction.time.monotonic", lambda: now[0])
    monkeypatch.setattr("opensquilla.session.compaction.sleep_before_retry", sleep)
    monkeypatch.setattr("opensquilla.provider.recovery.random.randint", lambda *_args: 0)
    monkeypatch.setattr(retry_after, "_provider_retry_after_cooldowns",
                        retry_after.ProviderRetryAfterCooldowns(clock=lambda: now[0]))
    return now, delays


def scripted(streams):
    remaining = iter(streams)
    provider = _Provider(lambda: _Stream(next(remaining)))
    target = CompactionExecutionTarget(provider, "openai", "provider/model")
    return provider, CompactionExecutionPlan(candidates=(target,))


def success():
    return [TextDeltaEvent(text="complete summary"), DoneEvent(stop_reason="stop", output_tokens=2)]


@pytest.mark.parametrize("code,hint,delay", [("429", None, 10), ("429", 35, 35),
                                           ("503", None, 1), ("503", 7, 7),
                                           ("connection_failed", None, 5)])
async def test_summary_recovers_bound_model_accounts_every_attempt_and_clears_partial(
    clock, code, hint, delay,
):
    status = int(code) if code.isdigit() else None
    provider, plan = scripted([
        [TextDeltaEvent(text="discard this partial "),
         ErrorEvent(code=code, message="rate limit" if code == "429" else "temporary failure",
                    retry_after_s=hint, http_status=status)], success(),
    ])
    activities = []
    starts = []
    sink = _Sink()
    with bind_usage_accounting_scope(_usage_scope(sink)):
        summary = await call_compaction_provider(
            "original history", "", plan, on_summary_recovery=activities.append,
            on_summary_call_started=lambda: starts.append(True),
        )
    assert summary == "complete summary"
    assert len(provider.calls) == len(starts) == len(sink.starts) == 2
    assert provider.calls[0][0] == provider.calls[1][0]
    assert all(stream.closed for stream in provider.streams)
    assert clock[1] == [delay]
    assert [event.phase for event in activities] == ["retry_wait", "retrying"]
    assert activities[0].http_status == status
    assert activities[1].http_status is None
    assert len(sink.finalized) + len(sink.unknown) == 2


async def test_429_exhaustion_is_four_calls_and_preserves_failure_fact(clock):
    provider, plan = scripted([
        [ErrorEvent(code="429", message="rate limit", http_status=429)] for _ in range(4)
    ])
    details = []
    summary = await call_compaction_provider("history", "", plan,
                                             on_summary_failure_detail=details.append)
    assert summary is None
    assert len(provider.calls) == 4
    assert clock[1] == [10, 20, 40]
    assert details[-1]["http_status"] == 429
    assert details[-1]["failure_kind"] == "rate_limited"


@pytest.mark.parametrize("code,kind,delays", [
    ("429", "rate_limited", [10, 20, 40]), ("503", "provider_overloaded", [1, 2, 4]),
])
async def test_legacy_status_code_keeps_classification_without_inventing_http_fact(
    clock, code, kind, delays,
):
    provider, plan = scripted([[ErrorEvent(code=code)] for _ in range(4)])
    details = []
    activities = []
    assert await call_compaction_provider(
        "history", "", plan, on_summary_failure_detail=details.append,
        on_summary_recovery=activities.append,
    ) is None
    assert len(provider.calls) == 4
    assert clock[1] == delays
    assert details[-1]["failure_kind"] == kind
    assert details[-1].get("http_status") is None
    assert all(event.http_status is None for event in activities)


@pytest.mark.parametrize("code,provider_code", [("401", ""), ("429", "insufficient_quota"),
                                               ("429", "usage_limit_reached")])
async def test_permanent_failures_do_not_retry(clock, code, provider_code):
    provider, plan = scripted([[ErrorEvent(code=code, http_status=int(code),
                                         provider_code=provider_code)]])
    assert await call_compaction_provider("history", "", plan) is None
    assert len(provider.calls) == 1
    assert clock[1] == []


async def test_wait_rejected_before_progress_when_not_enough_deadline(clock):
    provider, plan = scripted([[ErrorEvent(code="429", retry_after_s=35, http_status=429)]])
    activities = []
    assert await call_compaction_provider(
        "history", "", plan, deadline_at_monotonic=clock[0][0] + 20,
        on_summary_recovery=activities.append,
    ) is None
    assert len(provider.calls) == 1
    assert activities == []
    assert clock[1] == []


async def test_retry_wait_cancellation_has_no_second_request(monkeypatch):
    provider, plan = scripted([[ErrorEvent(code="429", http_status=429)]])
    waiting = asyncio.Event()

    async def sleep(_delay):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("opensquilla.session.compaction.sleep_before_retry", sleep)
    task = asyncio.create_task(call_compaction_provider("history", "", plan))
    await asyncio.wait_for(waiting.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(provider.calls) == 1
    assert provider.streams[0].closed


async def test_failed_compaction_leaves_full_history_and_structured_error(clock):
    provider, _plan = scripted([
        [ErrorEvent(code="429", message="rate limit", http_status=429)] for _ in range(4)
    ])
    entries = _entries(30)
    original = copy.deepcopy(entries)
    config = _suffix_config(provider)
    result = await compact_context(CompactionRequest(
        session_id="retry-history", entries=entries, context_window_tokens=500,
        config=config,
    ))
    assert entries == original
    assert result.kept_entries == original
    assert result.removed_count == 0
    assert result.summary == ""
    assert result.failure_detail["http_status"] == 429
    assert len(provider.calls) == 4


@pytest.mark.parametrize("failure", ["connection", "http429", "http503"])
async def test_extension_provider_raised_transient_error_uses_same_owner(clock, failure):
    attempts = []

    def factory():
        attempts.append(True)
        if len(attempts) == 1:
            request = httpx.Request("POST", "https://provider.invalid/chat")
            if failure == "connection":
                raise httpx.ConnectError("connection failed", request=request)
            status = 429 if failure == "http429" else 503
            response = httpx.Response(status, headers={"Retry-After": "8"}, request=request)
            raise httpx.HTTPStatusError("upstream failure", request=request, response=response)
        return _Stream(success())

    provider = _Provider(factory)
    plan = CompactionExecutionPlan(candidates=(
        CompactionExecutionTarget(provider, "openai", "provider/model"),
    ))
    assert await call_compaction_provider("history", "", plan) == "complete summary"
    assert len(attempts) == 2
    assert clock[1] == ([5] if failure == "connection" else [8])


@pytest.mark.parametrize("provider_code,kind", [
    ("usage_limit_reached", "usage_limit"), ("insufficient_quota", "insufficient_credits"),
])
async def test_extension_provider_non_rpm_429_is_not_retried(clock, provider_code, kind):
    from opensquilla.provider.retry_after import provider_retry_after_cooldowns

    def factory():
        response = httpx.Response(
            429, headers={"Retry-After": "35"}, json={"error": {"code": provider_code}},
            request=httpx.Request("POST", "https://provider.invalid/chat"),
        )
        response.raise_for_status()

    provider = _Provider(factory)
    plan = CompactionExecutionPlan(candidates=(
        CompactionExecutionTarget(provider, "openai", "provider/model"),
    ))
    failures = []
    assert await call_compaction_provider(
        "history", "", plan, on_summary_failure_detail=failures.append,
    ) is None
    assert len(provider.calls) == 1
    assert clock[1] == []
    assert failures[0]["failure_kind"] == kind
    assert failures[0]["http_status"] == 429
    assert provider_retry_after_cooldowns().remaining(provider) == 0


@pytest.mark.parametrize("compatibility_resend", [False, True])
async def test_native_adapter_recovery_preserves_chat_accounting_and_compatibility(
    monkeypatch, clock, compatibility_resend,
):
    from opensquilla.provider.openai import OpenAIProvider

    requests = []

    def handler(request):
        requests.append(json.loads(request.content)["stream"])
        if compatibility_resend and len(requests) == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=b"data: [DONE]\n\n")
        if len(requests) == (2 if compatibility_resend else 1):
            return httpx.Response(429, headers={"Retry-After": "8"},
                                  json={"error": {"code": "rate_limit_exceeded"}})
        frame = {"choices": [{"delta": {"content": "complete summary"},
                              "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n")

    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.pop("proxy", None)
        return original_client(*args, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    provider_kind = "moonshot" if compatibility_resend else "openrouter"
    provider = OpenAIProvider(api_key="test", model="provider/model", provider_kind=provider_kind,
                              base_url="https://provider.invalid/v1")
    plan = CompactionExecutionPlan(candidates=(
        CompactionExecutionTarget(provider, provider_kind, "provider/model"),
    ))
    sink = _Sink()
    chat_starts = []
    with bind_usage_accounting_scope(_usage_scope(sink)):
        assert await call_compaction_provider(
            "history", "", plan, on_summary_call_started=lambda: chat_starts.append(True),
        ) == "complete summary"
    assert requests == ([True, False, True] if compatibility_resend else [True, True])
    # UsageCallStart counts provider.chat calls. The adapter's stream/non-stream
    # compatibility resend remains inside one call, independently of HTTP count.
    assert len(chat_starts) == len(sink.starts) == 2
    assert len({call.event_id for call in sink.starts}) == 2
    assert len(sink.finalized) == 1
    assert len(sink.unknown) == 1
    settled_call, known = sink.finalized[0]
    assert (known.input_tokens, known.output_tokens) == (7, 3)
    assert settled_call.event_id == sink.starts[1].event_id
    assert sink.unknown[0][0].event_id == sink.starts[0].event_id
    assert clock[1] == [8]
