"""Bounded naming recovery keeps receipts, title ownership and safe diagnostics."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from opensquilla import token_estimation
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider import model_catalog, selector
from opensquilla.provider.protocol import ProviderConnectionConfig, ProviderMetadata
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.types import (
    DoneEvent,
    ErrorEvent,
    ProviderRequestCorrelation,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolArgumentRejection,
)
from opensquilla.session import naming
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage

_MODEL = "synthetic-naming-model"
_TITLE = "Inspect queue latency"
_KEY = "agent:main:webchat:synthetic-naming-recovery"


class _Stream:
    def __init__(self, events):
        self.events = iter(events)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            event = next(self.events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc
        if isinstance(event, BaseException):
            raise event
        return event

    async def aclose(self):
        self.closed = True


class _Provider:
    provider_name = "tokenrhythm"

    def __init__(self, scripts):
        self.scripts = scripts
        self.calls = []
        self.streams = []

    def provider_metadata(self):
        return ProviderMetadata(
            provider_name=self.provider_name,
            provider_kind="tokenrhythm",
            provider_id="tokenrhythm",
            model=_MODEL,
            base_url="https://naming.example.test/v1",
        )

    def provider_connection_config(self):
        return ProviderConnectionConfig(
            provider_kind="tokenrhythm",
            model=_MODEL,
            api_key="public-dummy-naming-key",
            base_url="https://naming.example.test/v1",
        )

    def chat(self, messages, tools=None, config=None):
        script = self.scripts[min(len(self.calls), len(self.scripts) - 1)]
        self.calls.append((messages, tools, config))
        stream = _Stream(script)
        self.streams.append(stream)
        return stream


class _Sink:
    def __init__(self):
        self.started = []
        self.finalized = []
        self.unknown = []

    async def start(self, call):
        self.started.append(call)

    async def finalize(self, call, result):
        self.finalized.append((call, result))

    async def mark_unknown(self, call, reason):
        self.unknown.append((call, reason))


class _Log:
    def __init__(self):
        self.records = []

    def info(self, event, **fields):
        self.records.append((event, fields))

    def warning(self, event, **fields):
        self.records.append((event, fields))


@pytest.fixture(autouse=True)
def isolated_budget(monkeypatch):
    monkeypatch.setattr(model_catalog, "_shared_catalog", model_catalog.ModelCatalog())
    monkeypatch.setattr(token_estimation, "_get_encoding", lambda: None)


@pytest.fixture
def retry_waits(monkeypatch):
    waits = []

    async def wait(delay):
        waits.append(delay)

    monkeypatch.setattr(naming, "_sleep_before_naming_retry", wait)
    return waits


def _success():
    return [
        TextDeltaEvent(text=_TITLE),
        DoneEvent(input_tokens=12, output_tokens=18, reasoning_tokens=8),
    ]


def _scope(sink):
    return UsageAccountingScope(
        sink=sink,
        context=UsageExecutionContext(
            execution_id="synthetic-naming-execution",
            agent_run_id="synthetic-naming-run",
            session_id="synthetic-naming-session",
            run_kind="session_naming",
        ),
    )


@pytest.mark.asyncio
async def test_conservative_reasoning_estimate_does_not_reject_completed_title():
    reasoning = "Review queue ordering. " * 200
    assert token_estimation.estimate_tokens(reasoning) > 2048
    provider = _Provider([[
        ReasoningDeltaEvent(text=reasoning),
        TextDeltaEvent(text=_TITLE),
        DoneEvent(input_tokens=12, output_tokens=577, reasoning_tokens=569),
    ]])
    sink = _Sink()

    with bind_usage_accounting_scope(_scope(sink)):
        title = await naming.call_naming_provider(
            "Inspect queue latency", provider=provider, model=_MODEL,
        )

    assert title == _TITLE
    assert len(provider.calls) == 1
    assert provider.calls[0][2].max_tokens == 2048
    assert provider.streams[0].closed
    assert len(sink.finalized) == 1
    assert sink.finalized[0][1].output_tokens == 577
    assert sink.finalized[0][1].reasoning_tokens == 569
    assert sink.unknown == []


@pytest.mark.asyncio
async def test_reasoning_default_provider_can_complete_within_2048_token_cap():
    provider = _Provider([[
        ReasoningDeltaEvent(text="Check request ordering. " * 150),
        TextDeltaEvent(text=_TITLE),
        DoneEvent(input_tokens=12, output_tokens=1292, reasoning_tokens=1282),
    ]])
    sink = _Sink()

    with bind_usage_accounting_scope(_scope(sink)):
        title = await naming.call_naming_provider(
            "Inspect queue latency", provider=provider, model=_MODEL,
        )

    assert title == _TITLE
    assert len(provider.calls) == 1
    assert provider.calls[0][2].max_tokens == 2048
    assert provider.streams[0].closed
    assert len(sink.finalized) == 1
    assert sink.finalized[0][1].output_tokens == 1292
    assert sink.finalized[0][1].reasoning_tokens == 1282
    assert sink.unknown == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_events", [
    [ErrorEvent(message="Upstream unavailable", code="503", http_status=503)],
    [DoneEvent(input_tokens=12, output_tokens=0)],
    [TextDeltaEvent(text="Unfinished candidate")],
])
async def test_transient_failure_retries_with_a_fresh_candidate(failed_events, retry_waits):
    provider = _Provider([failed_events, _success()])
    sink = _Sink()
    correlation = ProviderRequestCorrelation(
        session_id="synthetic-naming-session",
        turn_id="synthetic-naming-turn",
        execution_id="synthetic-initial-request",
        call_kind="auxiliary.naming",
    )

    with bind_usage_accounting_scope(_scope(sink)):
        title = await naming.call_naming_provider(
            "Inspect queue latency", provider=provider, model=_MODEL,
            provider_request_correlation=correlation,
        )

    assert title == _TITLE
    assert len(provider.calls) == 2
    assert len(retry_waits) == 1
    assert all(stream.closed for stream in provider.streams)
    assert all(call[1] is None and call[2].physical_attempt_limit == 1 for call in provider.calls)
    assert len(sink.started) == 2
    assert len({call.event_id for call in sink.started}) == 2
    assert [call.call_index for call in sink.started] == [1, 2]
    assert len(sink.finalized) + len(sink.unknown) == 2
    request_ids = [call[2].provider_request_correlation for call in provider.calls]
    assert request_ids[0] == correlation
    assert len({identity.execution_id for identity in request_ids}) == 2
    assert all(identity.session_id == correlation.session_id for identity in request_ids)
    assert all(identity.turn_id == correlation.turn_id for identity in request_ids)
    assert all(identity.call_kind == "auxiliary.naming" for identity in request_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_events", [
    [ErrorEvent(message="Upstream unavailable", code="503", http_status=503)],
    [DoneEvent(input_tokens=12, output_tokens=0)],
    [TextDeltaEvent(text="Unfinished candidate")],
])
async def test_persistent_transient_failure_stops_after_three_attempts(failed_events, retry_waits):
    provider = _Provider([failed_events])

    assert await naming.call_naming_provider(
        "Inspect queue latency", provider=provider, model=_MODEL,
    ) is None

    assert len(provider.calls) == 3
    assert len(retry_waits) == 2
    assert all(stream.closed for stream in provider.streams)


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [
    [ErrorEvent(message="Invalid credential", code="401", http_status=401)],
    [ErrorEvent(message="Insufficient credits", code="402", http_status=402)],
    [ErrorEvent(message="Invalid request", code="400", http_status=400)],
    [TextDeltaEvent(text="Cannot assist"), DoneEvent(refusal=True, output_tokens=4)],
    [TextDeltaEvent(text="First line\nSecond line"), DoneEvent(output_tokens=6)],
    [TextDeltaEvent(text=_TITLE), DoneEvent(output_tokens=2049)],
    [TextDeltaEvent(text=_TITLE), DoneEvent(output_tokens=12, reasoning_tokens=2049)],
    [TextDeltaEvent(text="Inspect queue"), DoneEvent(
        stop_reason="length", output_tokens=2048, reasoning_tokens=2030,
    )],
    [ReasoningDeltaEvent(text="x" * 65_537)],
    [ErrorEvent(
        message="Upstream unavailable", code="503", http_status=503, recovery_exhausted=True,
    )],
    [ErrorEvent(
        message="Incomplete tool arguments", code="incomplete_tool_call",
        tool_argument_rejection=ToolArgumentRejection(calls=(), terminal_reason="tool_calls"),
    )],
])
async def test_nonrecoverable_result_does_not_retry(events, retry_waits):
    provider = _Provider([events])

    assert await naming.call_naming_provider(
        "Inspect queue latency", provider=provider, model=_MODEL,
    ) is None

    assert len(provider.calls) == 1
    assert retry_waits == []
    assert provider.streams[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [
    [DoneEvent(input_tokens=12, output_tokens=0)],
    [httpx.RequestError("Synthetic transport failure")],
])
async def test_provider_replay_policy_prevents_retry(events, retry_waits):
    provider = _Provider([events, _success()])
    provider.retry_failed_call_safe = False

    assert await naming.call_naming_provider(
        "Inspect queue latency", provider=provider, model=_MODEL,
    ) is None

    assert len(provider.calls) == 1
    assert retry_waits == []
    assert provider.streams[0].closed


@pytest.mark.asyncio
async def test_retry_after_beyond_deadline_does_not_dispatch_early(retry_waits):
    provider = _Provider([[
        ErrorEvent(message="Rate limited", code="429", http_status=429, retry_after_s=60),
    ]])

    assert await naming.call_naming_provider(
        "Inspect queue latency", provider=provider, model=_MODEL, timeout=1,
    ) is None

    assert len(provider.calls) == 1
    assert retry_waits == []


@pytest.mark.asyncio
async def test_raw_http_status_error_preserves_retry_after_admission(retry_waits):
    request = httpx.Request("POST", "https://naming.example.test/v1/chat/completions")
    response = httpx.Response(503, headers={"Retry-After": "60"}, request=request)
    provider = _Provider([[
        httpx.HTTPStatusError("Synthetic upstream unavailable", request=request, response=response),
    ], _success()])

    assert await naming.call_naming_provider(
        "Inspect queue latency", provider=provider, model=_MODEL, timeout=1,
    ) is None

    assert len(provider.calls) == 1
    assert retry_waits == []
    assert provider.streams[0].closed


@pytest.mark.asyncio
async def test_continuation_guard_stops_another_physical_request(retry_waits):
    provider = _Provider([[
        ErrorEvent(message="Upstream unavailable", code="503", http_status=503),
    ], _success()])
    guard_calls = []

    async def should_continue():
        guard_calls.append(True)
        return len(guard_calls) == 1

    assert await naming.call_naming_provider(
        "Inspect queue latency", provider=provider, model=_MODEL,
        should_continue=should_continue,
    ) is None

    assert len(provider.calls) == 1
    assert len(guard_calls) >= 2
    assert provider.streams[0].closed


@pytest.mark.asyncio
async def test_cancellation_during_retry_wait_closes_stream_and_stops(monkeypatch):
    waiting = asyncio.Event()

    async def wait(_delay):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(naming, "_sleep_before_naming_retry", wait)
    provider = _Provider([[
        ErrorEvent(message="Upstream unavailable", code="503", http_status=503),
    ], _success()])
    task = asyncio.create_task(naming.call_naming_provider(
        "Inspect queue latency", provider=provider, model=_MODEL,
    ))
    await asyncio.wait_for(waiting.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(provider.calls) == 1
    assert provider.streams[0].closed


@pytest.mark.asyncio
async def test_failure_logs_use_closed_metadata_without_raw_provider_payload(
    monkeypatch, retry_waits,
):
    log = _Log()
    monkeypatch.setattr(naming, "log", log)
    secret = "public-dummy-sensitive-error-value"
    raw_message = f"https://naming.example.test/?key={secret} Inspect private request"
    provider = _Provider([[
        ErrorEvent(message=raw_message, code=secret, http_status=503),
    ]])

    assert await naming.call_naming_provider(
        "Inspect queue latency", provider=provider, model=_MODEL,
    ) is None

    serialized = json.dumps(log.records)
    assert secret not in serialized
    assert raw_message not in serialized
    failures = [
        fields for event, fields in log.records
        if event == "session_naming.provider_call_failed"
    ]
    assert failures
    for fields in failures:
        assert fields["reason_code"]
        assert fields["failure_kind"] == "provider_overloaded"
        assert fields["http_status"] == 503
        assert fields["error_code"] == "provider_error"
        assert fields["max_attempts"] == 3
        assert 1 <= fields["attempt"] <= 3
        assert isinstance(fields["retryable"], bool)
        assert "error" not in fields


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["manual_rename", "reset", "replacement", "delete"])
async def test_session_change_during_backoff_prevents_retry_and_title_commit(
    tmp_path, monkeypatch, change,
):
    provider = _Provider([[
        ErrorEvent(message="Upstream unavailable", code="503", http_status=503),
    ], _success()])
    monkeypatch.setattr(selector, "build_provider_from_config", lambda _config: provider)
    storage = SessionStorage(str(tmp_path / "synthetic-recovery.db"))
    await storage.connect()
    try:
        manager = SessionManager(storage)
        await storage.upsert_session(SessionNode(
            session_key=_KEY, session_id="synthetic-original-session", display_name="WebChat",
        ))

        async def wait(_delay):
            if change == "manual_rename":
                await manager.update(_KEY, display_name="Chosen by user")
            elif change == "reset":
                await manager.update(_KEY, epoch=1)
            elif change == "replacement":
                await storage.upsert_session(SessionNode(
                    session_key=_KEY, session_id="synthetic-replacement-session",
                    display_name="WebChat",
                ))
            else:
                await storage.delete_session(_KEY)

        monkeypatch.setattr(naming, "_sleep_before_naming_retry", wait)
        await naming.generate_session_title(
            SimpleNamespace(config=GatewayConfig(), session_manager=manager),
            _KEY,
            "Inspect queue latency",
            selected_provider_config=ProviderConfig(
                provider="tokenrhythm", model=_MODEL, api_key="public-dummy-naming-key",
                base_url="https://naming.example.test/v1",
            ),
            expected_session_id="synthetic-original-session",
            expected_session_epoch=0,
        )

        assert len(provider.calls) == 1
        current = await storage.get_session(_KEY)
        if change == "delete":
            assert current is None
        else:
            assert current is not None
            assert current.derived_title is None
            if change == "manual_rename":
                assert current.display_name == "Chosen by user"
    finally:
        await storage.close()
