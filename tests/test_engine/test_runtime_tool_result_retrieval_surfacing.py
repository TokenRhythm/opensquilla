from __future__ import annotations

from pathlib import Path

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import CallerKind, ToolContext


def _runner() -> TurnRunner:
    runner = TurnRunner(provider_selector=None, config=GatewayConfig())
    runner._tool_registry = get_default_registry()
    return runner


def _tool_names(runner: TurnRunner, ctx: ToolContext) -> set[str]:
    definitions, _handler = runner._build_tools(ctx)
    return {definition.name for definition in definitions}


def test_store_surfaces_retrieval_before_first_agent_schema(tmp_path: Path) -> None:
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        tool_result_store_dir=str(tmp_path / "tool-results"),
    )

    names = _tool_names(_runner(), ctx)

    assert "retrieve_tool_result" in names
    assert ctx.surfaced_tools is not None
    assert "retrieve_tool_result" in ctx.surfaced_tools
    assert ctx.tool_result_retrieval_available is True


def test_store_does_not_bypass_explicit_retrieval_allowlist(tmp_path: Path) -> None:
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        allowed_tools={"exec_command"},
        tool_result_store_dir=str(tmp_path / "tool-results"),
    )

    names = _tool_names(_runner(), ctx)

    assert "exec_command" in names
    assert "retrieve_tool_result" not in names
    assert ctx.tool_result_retrieval_available is False


def test_store_does_not_bypass_explicit_retrieval_deny(tmp_path: Path) -> None:
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        denied_tools={"retrieve_tool_result"},
        tool_result_store_dir=str(tmp_path / "tool-results"),
    )

    names = _tool_names(_runner(), ctx)

    assert "retrieve_tool_result" not in names
    assert ctx.tool_result_retrieval_available is False


def test_store_does_not_bypass_non_owner_channel_profile(
    tmp_path: Path,
) -> None:
    ctx = ToolContext(
        is_owner=False,
        caller_kind=CallerKind.CHANNEL,
        tool_result_store_dir=str(tmp_path / "tool-results"),
    )

    names = _tool_names(_runner(), ctx)

    assert "retrieve_tool_result" not in names
    assert ctx.tool_result_retrieval_available is False


def test_store_surfaces_retrieval_to_authenticated_non_owner_web_session(
    tmp_path: Path,
) -> None:
    ctx = ToolContext(
        is_owner=False,
        caller_kind=CallerKind.WEB,
        tool_result_store_dir=str(tmp_path / "tool-results"),
    )

    names = _tool_names(_runner(), ctx)

    assert "retrieve_tool_result" in names
    assert ctx.tool_result_retrieval_available is True
