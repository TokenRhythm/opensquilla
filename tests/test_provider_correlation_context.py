from __future__ import annotations

import asyncio

import pytest

from opensquilla.provider.correlation_context import (
    bind_provider_request_correlation,
    current_provider_request_correlation,
)
from opensquilla.provider.types import ProviderRequestCorrelation


def _correlation(label: str) -> ProviderRequestCorrelation:
    return ProviderRequestCorrelation(
        session_id=f"session-{label}",
        turn_id=f"turn-{label}",
        execution_id=f"execution-{label}",
        call_kind="agent.chat",
    )


def test_provider_correlation_scope_can_explicitly_clear_and_restore() -> None:
    parent = _correlation("parent")

    assert current_provider_request_correlation() is None
    with bind_provider_request_correlation(parent):
        assert current_provider_request_correlation() is parent
        with bind_provider_request_correlation(None):
            assert current_provider_request_correlation() is None
        assert current_provider_request_correlation() is parent
    assert current_provider_request_correlation() is None


@pytest.mark.asyncio
async def test_provider_correlation_scope_isolated_between_concurrent_tasks() -> None:
    first = _correlation("first")
    second = _correlation("second")

    async def observe(
        correlation: ProviderRequestCorrelation,
    ) -> ProviderRequestCorrelation | None:
        with bind_provider_request_correlation(correlation):
            await asyncio.sleep(0)
            return current_provider_request_correlation()

    observed = await asyncio.gather(observe(first), observe(second))

    assert observed == [first, second]
    assert current_provider_request_correlation() is None
