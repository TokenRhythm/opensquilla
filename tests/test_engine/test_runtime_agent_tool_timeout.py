from __future__ import annotations

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig


@pytest.mark.parametrize("explicit", [None, 0.0, 0.001, 44.0, -1.0])
def test_retired_tool_timeout_settings_are_inert(monkeypatch, explicit) -> None:
    monkeypatch.setenv("OPENSQUILLA_AGENT_TOOL_TIMEOUT", "22")
    runner = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(agent_tool_timeout_seconds=33.0),
    )
    assert runner._resolve_agent_tool_timeout("agent:main:test", explicit) == 0.0
