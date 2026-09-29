"""Regression pins for two stale-mainline tool contracts.

- web_discover is a registered tool, so it must stay in the cron-agent and
  concurrency allowlists (dropping it loses the tool for cron agents and
  serializes parallel calls).
- background_process must allow the same 90-minute ceiling that
  process(wait) expects; a lower background ceiling kills long builds early.
"""

from __future__ import annotations

from opensquilla.engine.runtime import _SAFE_TOOL_NAMES
from opensquilla.tools.builtin import shell
from opensquilla.tools.types import CRON_AGENT_ALLOW


def test_web_discover_stays_in_allowlists() -> None:
    assert "web_discover" in CRON_AGENT_ALLOW
    assert "web_discover" in _SAFE_TOOL_NAMES


def test_background_timeout_ceiling_covers_process_wait() -> None:
    # background_process must not clamp below the process(wait)
    # ceiling, or long-running builds get killed before the wait contract expects.
    assert shell._MAX_BACKGROUND_TIMEOUT >= shell._MAX_PROCESS_WAIT_TIMEOUT
    assert shell._MAX_BACKGROUND_TIMEOUT == 5400.0
    assert shell._resolve_background_timeout(5400.0) == 5400.0
    assert shell._resolve_process_wait_timeout(5400.0) == 5400.0
