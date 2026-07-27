from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from opensquilla.engine.turn_runner.harness import _TurnRunnerSessionTotalsAdapter
from opensquilla.engine.types import DoneEvent


class _Manager:
    def __init__(self) -> None:
        self.session = SimpleNamespace(
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            estimated_cost_usd=0.0,
            total_cost_usd=0.0,
            billed_cost_usd=0.0,
            estimated_cost_component_usd=0.0,
            cost_source="none",
            missing_cost_entries=0,
            cache_read=0,
            cache_write=0,
            model_override=None,
        )

    async def get_session(self, session_key: str):
        assert session_key == "agent:webchat:mixed-turn"
        return self.session

    async def update(self, session_key: str, **values):
        assert session_key == "agent:webchat:mixed-turn"
        for name, value in values.items():
            setattr(self.session, name, value)


class _Runner:
    def __init__(self) -> None:
        self._session_manager = _Manager()

    @asynccontextmanager
    async def _session_write_context(self, session_key: str):
        assert session_key == "agent:webchat:mixed-turn"
        yield


@pytest.mark.asyncio
async def test_session_totals_rollup_splits_mixed_turn_cost_components() -> None:
    runner = _Runner()
    adapter = _TurnRunnerSessionTotalsAdapter(runner)  # type: ignore[arg-type]
    done = DoneEvent(
        input_tokens=100,
        output_tokens=10,
        cost_usd=0.03,
        billed_cost=0.01,
        cost_source="mixed",
        model="deepseek/deepseek-v4-pro",
    )

    result = await adapter.rollup(
        session_key="agent:webchat:mixed-turn",
        done_event=done,
        resolved_model="deepseek/deepseek-v4-pro",
    )

    assert result is not None
    assert result.total_cost_usd == pytest.approx(0.03)
    assert result.billed_cost_usd == pytest.approx(0.01)
    assert result.estimated_cost_component_usd == pytest.approx(0.02)
    assert result.cost_source == "mixed"
    assert runner._session_manager.session.estimated_cost_component_usd == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_session_totals_rollup_preserves_confirmed_zero_source() -> None:
    runner = _Runner()
    adapter = _TurnRunnerSessionTotalsAdapter(runner)  # type: ignore[arg-type]

    result = await adapter.rollup(
        session_key="agent:webchat:mixed-turn",
        done_event=DoneEvent(
            input_tokens=10,
            output_tokens=1,
            cost_usd=0.0,
            billed_cost=0.0,
            cost_source="provider_billed",
        ),
        resolved_model="synthetic-model",
    )

    assert result is not None
    assert result.cost_source == "provider_billed"
    assert runner._session_manager.session.cost_source == "provider_billed"


@pytest.mark.asyncio
async def test_session_totals_rollup_mixes_confirmed_zero_with_estimate() -> None:
    runner = _Runner()
    adapter = _TurnRunnerSessionTotalsAdapter(runner)  # type: ignore[arg-type]
    await adapter.rollup(
        session_key="agent:webchat:mixed-turn",
        done_event=DoneEvent(cost_source="provider_billed"),
        resolved_model="synthetic-model",
    )

    result = await adapter.rollup(
        session_key="agent:webchat:mixed-turn",
        done_event=DoneEvent(
            input_tokens=10,
            output_tokens=1,
            cost_usd=0.25,
            billed_cost=0.0,
            cost_source="opensquilla_estimate",
        ),
        resolved_model="synthetic-model",
    )

    assert result is not None
    assert result.cost_source == "mixed"
