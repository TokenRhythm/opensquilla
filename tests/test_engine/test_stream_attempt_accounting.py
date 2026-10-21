from __future__ import annotations

from collections.abc import AsyncIterator

from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageCallResult,
    UsageCallStart,
    UsageExecutionContext,
    account_provider_stream,
    bind_usage_accounting_scope,
    usd_to_nanos,
)
from opensquilla.provider.types import DoneEvent, ErrorEvent, TextDeltaEvent


class _Sink:
    def __init__(self) -> None:
        self.starts: list[UsageCallStart] = []
        self.results: list[tuple[UsageCallStart, UsageCallResult]] = []
        self.unknown: list[tuple[UsageCallStart, str]] = []

    async def start(self, call: UsageCallStart) -> None:
        self.starts.append(call)

    async def finalize(self, call: UsageCallStart, result: UsageCallResult) -> None:
        self.results.append((call, result))

    async def mark_unknown(self, call: UsageCallStart, reason: str) -> None:
        self.unknown.append((call, reason))


async def _consume_replacement(sink: _Sink, failed: ErrorEvent) -> None:
    scope = UsageAccountingScope(
        sink=sink,
        context=UsageExecutionContext(
            execution_id="synthetic-execution", agent_run_id="synthetic-run",
        ),
    )

    async def interrupted() -> AsyncIterator[TextDeltaEvent | ErrorEvent]:
        yield TextDeltaEvent(text="synthetic partial")
        yield failed

    async def replacement() -> AsyncIterator[TextDeltaEvent | DoneEvent]:
        yield TextDeltaEvent(text="synthetic complete")
        yield DoneEvent(
            model="synthetic-model", input_tokens=10, output_tokens=2,
            billed_cost=0.02, cost_source="provider_billed",
        )

    with bind_usage_accounting_scope(scope):
        for stream in (interrupted, replacement):
            async for _event in account_provider_stream(
                stream, provider="synthetic-provider", model="synthetic-model",
            ):
                pass


async def test_failed_attempt_receipts_and_missing_usage_survive_successful_replacement() -> None:
    sink = _Sink()
    await _consume_replacement(
        sink,
        ErrorEvent(
            code="timeout", recovery_origin="read_timeout", usage_missing_count=1,
            model_usage_breakdown=[{
                "provider": "synthetic-provider", "model": "synthetic-model",
                "input_tokens": 11, "output_tokens": 3,
                "billed_cost": 0.03, "cost_source": "provider_billed",
            }],
        ),
    )

    assert [call.call_index for call in sink.starts] == [1, 2]
    assert len({call.event_id for call in sink.starts}) == 2
    assert [call.event_id for call, _ in sink.results] == [call.event_id for call in sink.starts]
    assert sum(result.billed_cost_nanos for _, result in sink.results) == usd_to_nanos(0.05)
    assert sum(result.missing_usage_entries for _, result in sink.results) == 1
    assert sink.unknown == []


async def test_missing_failed_attempt_receipt_is_unknown_even_after_replacement_succeeds() -> None:
    sink = _Sink()
    await _consume_replacement(sink, ErrorEvent(code="timeout", recovery_origin="read_timeout"))

    assert len(sink.starts) == 2
    assert [call.event_id for call, _ in sink.unknown] == [sink.starts[0].event_id]
    assert [call.event_id for call, _ in sink.results] == [sink.starts[1].event_id]
    assert sink.results[0][1].billed_cost_nanos == usd_to_nanos(0.02)
