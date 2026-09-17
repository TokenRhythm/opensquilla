from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.engine.runtime import (
    TurnRunner,
    _resolve_identity_prompt_mode,
)
from opensquilla.gateway.config import GatewayConfig


def test_identity_prompt_mode_auto_preserves_full_default() -> None:
    assert _resolve_identity_prompt_mode(GatewayConfig()) == "full"


def test_identity_prompt_mode_auto_preserves_memory_only_minimal() -> None:
    cfg = GatewayConfig(tools={"profile": "memory_only"})

    assert _resolve_identity_prompt_mode(cfg) == "minimal"


def test_identity_prompt_mode_explicit_value_overrides_auto_tool_profile() -> None:
    cfg = GatewayConfig(
        prompt={"mode": "headless_source_edit"},
        tools={"profile": "memory_only"},
    )

    assert _resolve_identity_prompt_mode(cfg) == "headless_source_edit"


def test_identity_prompt_mode_accepts_headless_repo_coding_scaffold() -> None:
    cfg = GatewayConfig(prompt={"mode": "headless_repo_coding_scaffold"})

    assert _resolve_identity_prompt_mode(cfg) == "headless_repo_coding_scaffold"


def test_identity_prompt_mode_short_env_alias_overrides_config(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROMPT_MODE", "headless_source_edit")
    cfg = GatewayConfig(prompt={"mode": "auto"})

    assert _resolve_identity_prompt_mode(cfg) == "headless_source_edit"


def test_identity_prompt_mode_env_accepts_headless_repo_coding_scaffold(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROMPT_MODE", "headless_repo_coding_scaffold")
    cfg = GatewayConfig(prompt={"mode": "auto"})

    assert _resolve_identity_prompt_mode(cfg) == "headless_repo_coding_scaffold"


@pytest.mark.parametrize("env_value", [None, "0", "1", "on", "garbage"])
@pytest.mark.parametrize("legacy_style", [False, True])
def test_retired_legacy_prompt_inputs_do_not_change_runtime_prompt(
    monkeypatch, tmp_path, env_value, legacy_style
) -> None:
    monkeypatch.delenv("OPENSQUILLA_LEGACY_PROMPT_STYLE", raising=False)
    runner = TurnRunner(provider_selector=None, config=GatewayConfig())
    monkeypatch.setattr(runner, "_resolve_bootstrap_workspace_dir", lambda _: tmp_path)
    monkeypatch.setattr(runner, "_resolve_memory_source_dir", lambda _: tmp_path)
    tool_defs = [SimpleNamespace(name="exec_command")]
    expected = runner._assemble_prompt("main", tool_defs)

    if env_value is not None:
        monkeypatch.setenv("OPENSQUILLA_LEGACY_PROMPT_STYLE", env_value)
    runner._config = GatewayConfig(prompt={"legacy_prompt_style": legacy_style})

    assert runner._assemble_prompt("main", tool_defs) == expected


@pytest.mark.parametrize("env_value", [None, "off", "on", "invalid-legacy-value"])
def test_retired_prompt_protocol_env_and_saved_config_are_inert(monkeypatch, tmp_path, env_value):
    monkeypatch.delenv("OPENSQUILLA_PATCH_EVIDENCE_PROTOCOL", raising=False)
    runner = TurnRunner(provider_selector=None, config=GatewayConfig())
    monkeypatch.setattr(runner, "_resolve_bootstrap_workspace_dir", lambda _: tmp_path)
    monkeypatch.setattr(runner, "_resolve_memory_source_dir", lambda _: tmp_path)
    definitions = [SimpleNamespace(name="exec_command")]
    expected = runner._assemble_prompt("main", definitions)
    if env_value is not None:
        monkeypatch.setenv("OPENSQUILLA_PATCH_EVIDENCE_PROTOCOL", env_value)
    runner._config = GatewayConfig(prompt={"patch_evidence_protocol": True})
    assert runner._assemble_prompt("main", definitions) == expected


@pytest.mark.parametrize("env_value", ["on", "invalid-legacy-value", "/missing/overrides.toml"])
def test_retired_description_overrides_do_not_change_tool_schemas(monkeypatch, env_value):
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext

    monkeypatch.setenv("OPENSQUILLA_TOOL_DESCRIPTION_OVERRIDES", env_value)
    registry = get_default_registry()
    baseline = ToolContext(is_owner=True, scratch_dir="/tmp/synthetic-scratch")
    legacy = ToolContext(
        is_owner=True,
        scratch_dir="/tmp/synthetic-scratch",
        tool_description_overrides={"exec_command": "obsolete", "exec_command.command": "obsolete"},
        tool_description_overrides_source="env_file",
    )
    assert registry.to_tool_definitions(legacy) == registry.to_tool_definitions(baseline)
    config = GatewayConfig(tools={"description_overrides": legacy.tool_description_overrides})
    runner = TurnRunner(provider_selector=None, config=config)
    runner._tool_registry = registry
    assert runner._build_tools(legacy)[0] == runner._build_tools(baseline)[0]
