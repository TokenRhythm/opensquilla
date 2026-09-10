"""Retired submit must stay absent without hiding formal workflow controls."""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig
from opensquilla.tools.registry import DEFAULT_MODEL_TOOL_NAMES, get_default_registry
from opensquilla.tools.types import ToolContext

# The active SWE profile: exactly these ten tools, no ``submit``.
_SCAFFOLD_TOOLS = frozenset(
    {
        "exec_command",
        "read_file",
        "edit_file",
        "write_file",
        "glob_search",
        "grep_search",
        "list_dir",
        "git_status",
        "git_diff",
        "retrieve_tool_result",
    }
)
_MODEL_SCAFFOLD_TOOLS = (_SCAFFOLD_TOOLS & DEFAULT_MODEL_TOOL_NAMES) | {"tool_search"}


def _runner_with_scaffold_profile() -> TurnRunner:
    config = GatewayConfig(tools={"profile": "repo_coding_scaffold_edit"})
    runner = TurnRunner(provider_selector=None, config=config)
    runner._tool_registry = get_default_registry()
    return runner


@pytest.mark.parametrize("retired_value", [None, "", "off", "on", "1"])
def test_retired_submit_env_does_not_expand_scaffold_tool_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retired_value: str | None,
) -> None:
    if retired_value is None:
        monkeypatch.delenv("OPENSQUILLA_SUBMIT_REVIEW", raising=False)
    else:
        monkeypatch.setenv("OPENSQUILLA_SUBMIT_REVIEW", retired_value)
    runner = _runner_with_scaffold_profile()

    ctx = ToolContext(is_owner=True, workspace_dir=str(tmp_path))
    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(td, "name", "") for td in tool_defs}

    assert names == _MODEL_SCAFFOLD_TOOLS
    assert "submit" not in names
    assert ctx.surfaced_tools is None or "submit" not in ctx.surfaced_tools
    assert ctx.allowed_tools is None or "submit" not in ctx.allowed_tools


def test_build_tools_exposes_plan_run_delivery_controls_under_scaffold_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_SUBMIT_REVIEW", raising=False)
    runner = _runner_with_scaffold_profile()

    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        plan_run_id="run-1",
    )
    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(td, "name", "") for td in tool_defs}

    assert "plan_run_checkpoint" in names
    assert "publish_artifact" in names
    plan_run_tools = {"plan_run_checkpoint", "publish_artifact"}
    assert names == _MODEL_SCAFFOLD_TOOLS | plan_run_tools
    assert ctx.surfaced_tools is not None
    assert plan_run_tools <= ctx.surfaced_tools


def test_build_tools_plan_run_ignores_retired_submit_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_SUBMIT_REVIEW", "on")
    runner = _runner_with_scaffold_profile()

    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        plan_run_id="run-1",
    )
    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(td, "name", "") for td in tool_defs}

    assert {"plan_run_checkpoint", "publish_artifact"} <= names
    assert "submit" not in names
    assert ctx.surfaced_tools is not None
    assert "submit" not in ctx.surfaced_tools


def test_build_tools_exposes_goal_controls_under_scaffold_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_SUBMIT_REVIEW", raising=False)
    runner = _runner_with_scaffold_profile()
    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        goal_context={"goalId": "goal-1"},
        goal_service=object(),
    )

    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(definition, "name", "") for definition in tool_defs}

    goal_tools = {"update_goal", "update_goal_progress"}
    assert goal_tools <= names
    assert names == _MODEL_SCAFFOLD_TOOLS | goal_tools
    assert ctx.surfaced_tools is not None
    assert goal_tools <= ctx.surfaced_tools


def test_build_tools_goal_control_explicit_deny_remains_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_SUBMIT_REVIEW", raising=False)
    runner = _runner_with_scaffold_profile()
    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        goal_context={"goalId": "goal-1"},
        goal_service=object(),
        denied_tools={"update_goal"},
    )

    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(definition, "name", "") for definition in tool_defs}

    assert "update_goal" not in names
    assert "update_goal_progress" in names


@pytest.mark.parametrize("allowed_tools", [{"submit"}, set()])
def test_retired_submit_allowlist_does_not_become_unrestricted(
    tmp_path: Path, allowed_tools: set[str]
) -> None:
    runner = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(tools={"also_allow": ["submit"]}),
    )
    runner._tool_registry = get_default_registry()
    ctx = ToolContext(is_owner=True, workspace_dir=str(tmp_path), allowed_tools=allowed_tools)

    tool_defs, _handler = runner._build_tools(ctx)

    # The existing runtime always adds its authorized-tool discovery control,
    # even to an empty allowlist; it must not expose any executable tool.
    assert {definition.name for definition in tool_defs} == {"tool_search"}
    assert ctx.allowed_tools is not None
    assert not (ctx.allowed_tools - {"submit", "tool_search"})
    assert {definition.name for definition in runner._tool_registry.to_tool_definitions(ctx)} == {
        "tool_search"
    }
