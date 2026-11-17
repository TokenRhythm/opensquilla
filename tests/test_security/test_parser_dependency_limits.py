"""Offline resource-boundary regressions for base parsing dependencies."""

from __future__ import annotations

import pytest
import soupsieve
from bs4 import BeautifulSoup
from opentelemetry.trace import get_current_span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


@pytest.mark.parametrize("as_list", [False, True], ids=["string", "list"])
def test_trace_context_bounds_future_version_extensions(as_list: bool) -> None:
    propagator = TraceContextTextMapPropagator()
    prefix = f"01-{'1' * 32}-{'2' * 16}-01"

    def extract(value: str):
        return get_current_span(propagator.extract({
            "traceparent": [value] if as_list else value,
        })).get_span_context()

    # Future versions may carry extensions; short valid values still propagate.
    ordinary = extract(prefix + "-extension")
    assert ordinary.is_valid
    assert ordinary.is_remote
    assert ordinary.trace_id == int("1" * 32, 16)
    assert ordinary.span_id == int("2" * 16, 16)
    assert ordinary.trace_flags.sampled
    # Reject an oversized extension before the parser's regex gets to process it.
    assert not extract(prefix + "-" + "a" * 1024).is_valid


@pytest.mark.parametrize("pseudo_class", ["is", "not", "where"])
def test_selector_nesting_preserves_matching_and_token_limit(pseudo_class: str) -> None:
    soup = BeautifulSoup('<a class="ordinary">Result</a>', "html.parser")
    assert soup.select_one(":is(:where(a)):not(.excluded)") is soup.a
    nested = f":{pseudo_class}(" * 500 + "a" + ")" * 500
    # Iterative parsing preserves valid deep selectors without exhausting Python's stack.
    assert soupsieve.compile(nested).match(soup.a)
    over_limit = f":{pseudo_class}(" * 8193 + "a" + ")" * 8193
    with pytest.raises(ValueError, match="nesting limit"):
        soupsieve.compile(over_limit)
