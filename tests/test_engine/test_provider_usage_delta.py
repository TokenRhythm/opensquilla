from dataclasses import replace

import pytest

from opensquilla.engine.provider_usage_delta import ProviderUsageDelta
from opensquilla.provider.types import DoneEvent, ErrorEvent, ProviderGenerationResetEvent


def test_cumulative_error_done_and_terminal_reset_count_new_rows_once():
    delta = ProviderUsageDelta()
    first = {"provider": "a", "model": "one", "input_tokens": 10, "output_tokens": 2}
    second = {"provider": "a", "model": "two", "input_tokens": 3, "output_tokens": 4}
    error = ErrorEvent(
        model_usage_breakdown=[first], usage_missing_count=1, cumulative_usage_id="turn-A",
    )
    assert delta.consume(error).model_usage_breakdown == [first]
    done = DoneEvent(
        input_tokens=13, output_tokens=6, model_usage_breakdown=[first, second],
        usage_missing_count=1, cumulative_usage_id="turn-A", terminal_request_input_tokens=3,
    )
    done._opensquilla_usage_model = "two"
    projected = delta.consume(done)
    assert projected.input_tokens == 3
    assert projected.output_tokens == 4
    assert projected.usage_missing_count == 0
    assert projected.terminal_request_input_tokens == 3
    assert projected._opensquilla_usage_model == "two"
    assert done.input_tokens == 13
    reset = ProviderGenerationResetEvent(
        terminal=True, model_usage_breakdown=[first, second],
        usage_missing_count=2, cumulative_usage_id="turn-A",
    )
    projected_reset = delta.consume(reset)
    assert projected_reset.model_usage_breakdown == []
    assert projected_reset.usage_missing_count == 1
    # Incremental receipts and a new independent scope remain untouched.
    assert delta.consume(replace(done, cumulative_usage_id="")).input_tokens == 13
    assert delta.consume(replace(done, cumulative_usage_id="turn-B")).input_tokens == 13


def test_cumulative_receipts_cannot_rewrite_previously_reported_usage():
    delta = ProviderUsageDelta()
    delta.consume(ErrorEvent(
        model_usage_breakdown=[{"input_tokens": 10}], cumulative_usage_id="same",
    ))
    with pytest.raises(ValueError, match="changed previously reported"):
        delta.consume(ErrorEvent(
            model_usage_breakdown=[{"input_tokens": 9}], cumulative_usage_id="same",
        ))
