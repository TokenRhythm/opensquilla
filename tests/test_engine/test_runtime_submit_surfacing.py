"""Retired submit must stay absent without hiding formal workflow controls."""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig
from opensquilla.tools.registry import DEFAULT_MODEL_TOOL_NAMES, get_default_registry
from opensquilla.tools.types import CallerKind, ToolContext

# An explicit repository tool allowlist must remain authoritative.
_REPOSITORY_TOOLS = frozenset(
    {
        "exec_command",
        "process",
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
_MODEL_REPOSITORY_TOOLS = (_REPOSITORY_TOOLS & DEFAULT_MODEL_TOOL_NAMES) | {"tool_search"}


def _runner_with_repository_allowlist() -> TurnRunner:
    config = GatewayConfig(tools={
        "profile": "minimal",
        "allow": sorted(_REPOSITORY_TOOLS),
        "deny": ["session_status"],
    })
    runner = TurnRunner(provider_selector=None, config=config)
    runner._tool_registry = get_default_registry()
    return runner


@pytest.mark.parametrize("retired_value", [None, "", "off", "on", "1"])
def test_retired_submit_env_does_not_expand_repository_tool_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retired_value: str | None,
) -> None:
    if retired_value is None:
        monkeypatch.delenv("OPENSQUILLA_SUBMIT_REVIEW", raising=False)
    else:
        monkeypatch.setenv("OPENSQUILLA_SUBMIT_REVIEW", retired_value)
    runner = _runner_with_repository_allowlist()

    ctx = ToolContext(is_owner=True, workspace_dir=str(tmp_path))
    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(td, "name", "") for td in tool_defs}

    assert names == _MODEL_REPOSITORY_TOOLS
    assert "submit" not in names
    assert ctx.surfaced_tools is None or "submit" not in ctx.surfaced_tools
    assert ctx.allowed_tools is None or "submit" not in ctx.allowed_tools


def test_plan_run_preserves_repository_tool_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_SUBMIT_REVIEW", raising=False)
    runner = _runner_with_repository_allowlist()

    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        plan_run_id="run-1",
    )
    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(td, "name", "") for td in tool_defs}

    assert "update_plan" not in names
    assert "publish_artifact" not in names
    assert names == _MODEL_REPOSITORY_TOOLS
    assert ctx.surfaced_tools is not None
    assert "update_plan" in ctx.surfaced_tools


async def _preview_opener(*args, **kwargs):
    return {"resourceId": "document:test"}


@pytest.mark.parametrize("plan_run", [False, True])
@pytest.mark.parametrize("supported", [False, True])
def test_preview_is_exposed_by_default_only_with_web_capability(
    tmp_path: Path, plan_run: bool, supported: bool
) -> None:
    runner = (
        _runner_with_repository_allowlist()
        if plan_run
        else TurnRunner(provider_selector=None, config=GatewayConfig())
    )
    runner._tool_registry = get_default_registry()
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
        plan_run_id="run-1" if plan_run else None,
        workspace_preview_opener=_preview_opener if supported else None,
    )

    definitions, _handler = runner._build_tools(ctx)

    expected = supported and not plan_run
    assert ("open_workspace_preview" in {tool.name for tool in definitions}) is expected
    assert ("open_workspace_preview" in ctx.authorized_tool_names) is (supported and not plan_run)


@pytest.mark.parametrize("caller_kind", [CallerKind.AGENT, CallerKind.CHANNEL, CallerKind.CRON])
def test_preview_is_not_exposed_to_non_web_callers(caller_kind: CallerKind) -> None:
    runner = TurnRunner(provider_selector=None, config=GatewayConfig())
    runner._tool_registry = get_default_registry()
    ctx = ToolContext(
        is_owner=True,
        caller_kind=caller_kind,
        workspace_preview_opener=_preview_opener,
        surfaced_tools={"open_workspace_preview"},
    )

    definitions, _handler = runner._build_tools(ctx)

    assert "open_workspace_preview" not in {tool.name for tool in definitions}
    assert "open_workspace_preview" not in ctx.authorized_tool_names


def test_preview_explicit_deny_wins_over_plan_run_surfacing() -> None:
    runner = _runner_with_repository_allowlist()
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_preview_opener=_preview_opener,
        plan_run_id="run-1",
        denied_tools={"open_workspace_preview"},
    )

    definitions, _handler = runner._build_tools(ctx)

    assert "open_workspace_preview" not in {tool.name for tool in definitions}


def test_build_tools_plan_run_ignores_retired_submit_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_SUBMIT_REVIEW", "on")
    runner = _runner_with_repository_allowlist()

    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        plan_run_id="run-1",
    )
    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(td, "name", "") for td in tool_defs}

    assert {"update_plan", "publish_artifact"}.isdisjoint(names)
    assert "submit" not in names
    assert ctx.surfaced_tools is not None
    assert "submit" not in ctx.surfaced_tools


def test_goal_controls_preserve_repository_tool_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_SUBMIT_REVIEW", raising=False)
    runner = _runner_with_repository_allowlist()
    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        goal_context={"goalId": "goal-1"},
        goal_service=object(),
    )

    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(definition, "name", "") for definition in tool_defs}

    goal_tools = {"update_goal", "update_plan"}
    assert goal_tools.isdisjoint(names)
    assert names == _MODEL_REPOSITORY_TOOLS
    assert ctx.surfaced_tools is not None
    assert goal_tools <= ctx.surfaced_tools


def test_build_tools_goal_control_explicit_deny_remains_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_SUBMIT_REVIEW", raising=False)
    runner = _runner_with_repository_allowlist()
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
    assert "update_plan" not in names


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
