"""TurnRunner._build_tools must actually expose ``submit`` under a
restrictive profile allowlist when submit-review is enabled.

``submit`` is registered with ``exposed_by_default=False``. Enabling
submit-review adds it to ``ctx.surfaced_tools``, which lifts the
exposed-by-default gate — but, by design, ``surfaced_tools`` does NOT
relax the ``allowed_tools`` allowlist (see ``ToolContext.surfaced_tools``).
Under the SWE profile ``repo_coding_scaffold_edit`` the allowlist is the
10 scaffold tools, which omit ``submit``; so surfacing ALONE leaves the
tool filtered as ``not_allowed`` and it never reaches the provider schema.

Two prior paid SWE runs were inert for exactly this reason
(provider_tool_schema tool_count=10, ``submit`` absent). ``_build_tools``
must therefore also add ``submit`` to the allowlist when the lever is on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig
from opensquilla.tools.registry import get_default_registry
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


def _runner_with_scaffold_profile() -> TurnRunner:
    config = GatewayConfig(tools={"profile": "repo_coding_scaffold_edit"})
    runner = TurnRunner(provider_selector=None, config=config)
    runner._tool_registry = get_default_registry()
    return runner


def test_build_tools_exposes_submit_under_scaffold_profile_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_SUBMIT_REVIEW", "on")
    runner = _runner_with_scaffold_profile()

    ctx = ToolContext(is_owner=True, workspace_dir=str(tmp_path))
    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(td, "name", "") for td in tool_defs}

    assert "submit" in names, (
        "submit must reach the tool surface under the scaffold profile when "
        f"submit-review is on; got {sorted(names)}"
    )
    # The tool surface is the 10 scaffold tools + submit == 11.
    assert _SCAFFOLD_TOOLS <= names
    assert len(names & (_SCAFFOLD_TOOLS | {"submit"})) == 11
    # surfaced_tools is mutated on the passed ctx before the policy step
    # reassigns it; the allowlist add happens on the internal (replaced) ctx,
    # so it is observable through the returned tool_defs above, not this ref.
    assert ctx.surfaced_tools is not None and "submit" in ctx.surfaced_tools


def test_build_tools_omits_submit_under_scaffold_profile_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_SUBMIT_REVIEW", raising=False)
    runner = _runner_with_scaffold_profile()

    ctx = ToolContext(is_owner=True, workspace_dir=str(tmp_path))
    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(td, "name", "") for td in tool_defs}

    assert "submit" not in names, (
        f"submit must stay hidden when submit-review is off; got {sorted(names)}"
    )
    assert ctx.allowed_tools is None or "submit" not in ctx.allowed_tools


def test_build_tools_env_off_overrides_config_on_under_scaffold_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Env off wins even if a config field would enable it (mirrors the
    # finalize-gate lever precedence).
    monkeypatch.setenv("OPENSQUILLA_SUBMIT_REVIEW", "off")
    runner = _runner_with_scaffold_profile()

    ctx = ToolContext(is_owner=True, workspace_dir=str(tmp_path))
    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(td, "name", "") for td in tool_defs}

    assert "submit" not in names


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
    assert _SCAFFOLD_TOOLS <= names
    plan_run_tools = {"plan_run_checkpoint", "publish_artifact"}
    assert len(names & (_SCAFFOLD_TOOLS | plan_run_tools)) == 12
    assert ctx.surfaced_tools is not None
    assert plan_run_tools <= ctx.surfaced_tools


def test_build_tools_plan_run_hides_submit_when_review_enabled(
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
    assert "submit" in ctx.denied_tools
