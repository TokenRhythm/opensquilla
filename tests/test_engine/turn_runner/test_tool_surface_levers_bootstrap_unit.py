"""Bootstrap-stage compatibility for tool-surface controls."""

from __future__ import annotations

import pytest

from opensquilla.engine.types import AgentConfig

from .test_agent_bootstrap_stage_unit import _make_input, _make_stage

_ENV = "OPENSQUILLA_PROJECTION_SIGNAL_HINTS"


def test_agent_config_defaults_keep_tool_surface_levers_off() -> None:
    config = AgentConfig()
    assert config.projection_signal_hints is False
    # The dead-field regression guard: dispatch/runtime-resolved levers must
    # not grow AgentConfig fields that nothing consumes.
    assert not hasattr(config, "placeholder_copy_escalation_threshold")
    assert not hasattr(config, "repeated_call_notice_threshold")
    assert not hasattr(config, "tool_description_overrides_file")


@pytest.mark.asyncio
async def test_retired_projection_signal_hints_env_is_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = _make_stage()
    monkeypatch.delenv(_ENV, raising=False)
    default_out = await stage.run(_make_input())
    assert default_out.output.agent_config.projection_signal_hints is False

    monkeypatch.setenv(_ENV, "on")
    enabled_out = await stage.run(_make_input())
    assert enabled_out.output.agent_config.projection_signal_hints is False

    monkeypatch.setenv(_ENV, "off")
    disabled_out = await stage.run(_make_input())
    assert disabled_out.output.agent_config.projection_signal_hints is False


@pytest.mark.asyncio
async def test_retired_projection_options_cannot_fail_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = _make_stage()
    monkeypatch.setenv(_ENV, "enabled")
    monkeypatch.setenv("OPENSQUILLA_PROJECTION_SIGNAL_PATTERNS", "[")
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_HISTORY_DEDUP", "on")
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_HISTORY_DEDUP_MIN_REPEATS", "invalid")
    monkeypatch.setenv("OPENSQUILLA_TOOL_RESULT_FRESH_DIAGNOSTIC_POLICY_ENABLED", "on")
    monkeypatch.setenv("OPENSQUILLA_TOOL_RESULT_DIAGNOSTIC_RETRIEVAL_GATE_ENABLED", "on")
    monkeypatch.setenv("OPENSQUILLA_TOOL_RESULT_FRESH_DIAGNOSTIC_INLINE_MAX_CHARS", "invalid")
    out = await stage.run(_make_input())
    assert out.output.agent_config.projection_signal_hints is False
    assert out.output.agent_config.provider_history_dedup_enabled is False
    assert out.output.agent_config.provider_history_dedup_min_repeats == 2
    assert out.output.agent_config.tool_result_fresh_diagnostic_policy_enabled is False
    assert out.output.agent_config.tool_result_diagnostic_retrieval_gate_enabled is False
