from __future__ import annotations

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.turn_runner.harness import _TurnRunnerTimeoutBudgetAdapter
from opensquilla.gateway.config import GatewayConfig


@pytest.mark.parametrize("legacy_timeout", [None, 0.0, 0.001, 44.0, -1.0])
def test_retired_tool_timeout_settings_leave_active_budgets_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    legacy_timeout: float | None,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_AGENT_TOOL_TIMEOUT", "22")
    runner = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(agent_tool_timeout_seconds=legacy_timeout),
    )
    budgets = _TurnRunnerTimeoutBudgetAdapter(runner).resolve_budgets(
        session_key="agent:main:test",
        timeout=42.0,
        max_iterations=2,
        request_timeout=11.0,
        max_provider_retries=5,
    )
    assert budgets.runtime_timeout == 42.0
    assert budgets.max_iterations == 2
    assert budgets.request_timeout == 11.0
    assert budgets.max_provider_retries == 5
