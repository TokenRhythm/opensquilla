"""Bootstrap-stage env threading for the tool-surface levers.

Covers OPENSQUILLA_PROJECTION_SIGNAL_HINTS (off by default). Motivation: this
knob must flow through the single sanctioned parse site
(agent_bootstrap_stage) into AgentConfig with the SAME on/off vocabulary the
runtime gate uses, so an unrecognized value fails at bootstrap instead of
surviving as False and then raising mid-turn when the agent re-reads the env.
The other tool-surface levers intentionally have no AgentConfig field:
OPENSQUILLA_REPEATED_CALL_NOTICE is read from the environment by the dispatch
layer per call, and OPENSQUILLA_TOOL_DESCRIPTION_OVERRIDES is resolved by the
runtime per turn — a config field for either would be dead state the manifest
could misreport.
"""

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
async def test_projection_signal_hints_env_threads_to_agent_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = _make_stage()
    monkeypatch.delenv(_ENV, raising=False)
    default_out = await stage.run(_make_input())
    assert default_out.output.agent_config.projection_signal_hints is False

    monkeypatch.setenv(_ENV, "on")
    enabled_out = await stage.run(_make_input())
    assert enabled_out.output.agent_config.projection_signal_hints is True

    monkeypatch.setenv(_ENV, "off")
    disabled_out = await stage.run(_make_input())
    assert disabled_out.output.agent_config.projection_signal_hints is False


@pytest.mark.asyncio
async def test_projection_signal_hints_unrecognized_value_raises_at_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same vocabulary as the runtime gate: "enabled" is recognized by the
    # generic _bool_from_env truthy set but NOT by the projection gate, so
    # accepting it here would arm the manifest while the runtime raises
    # mid-turn. It must fail at bootstrap instead.
    stage = _make_stage()
    monkeypatch.setenv(_ENV, "enabled")
    with pytest.raises(ValueError, match=_ENV):
        await stage.run(_make_input())
